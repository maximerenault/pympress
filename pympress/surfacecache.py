# -*- coding: utf-8 -*-
#
#       surfacecache.py
#
#       Copyright 2010 Thomas Jost <thomas.jost@gmail.com>
#       Copyright 2015 Cimbali <me@cimba.li>
#
#       This program is free software; you can redistribute it and/or modify
#       it under the terms of the GNU General Public License as published by
#       the Free Software Foundation; either version 2 of the License, or
#       (at your option) any later version.
#
#       This program is distributed in the hope that it will be useful,
#       but WITHOUT ANY WARRANTY; without even the implied warranty of
#       MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#       GNU General Public License for more details.
#
#       You should have received a copy of the GNU General Public License
#       along with this program; if not, write to the Free Software
#       Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#       MA 02110-1301, USA.
"""
:mod:`pympress.surfacecache` -- pages prerendering and caching
--------------------------------------------------------------

This modules contains stuff needed for caching pages and prerendering them. This
is done by the :class:`~pympress.surfacecache.SurfaceCache` class, using several
`dict` of :class:`~cairo.ImageSurface` for storing rendered pages.

The problem is, neither Gtk+ nor Poppler are particularly threadsafe.
Hence prerendering is done in a background thread that opens the document
a second time, so that no Poppler object is shared with the main thread.
"""

import logging
logger = logging.getLogger(__name__)

import threading
import functools
import collections

import gi
import cairo
gi.require_version('Gtk', '3.0')
gi.require_version('Poppler', '0.18')
from gi.repository import GLib, Poppler

from pympress import document


class SurfaceCache(object):
    """ Pages caching and prerendering made (almost) easy.

    Args:
        doc (:class:`~pympress.document.Document`):  the current document
        max_pages (`int`): The maximum page number.
    """

    #: The actual cache. The `dict`s keys are widget names and its values are
    #: :class:`~collections.OrderedDict`, whose keys are page numbers
    #: and values are instances of :class:`~cairo.ImageSurface`.
    #: In each :class:`~collections.OrderedDict` keys are ordered by
    #: Least Recently Used (get or set), when the size is beyond
    #: :attr:`max_pages`, pages are popped from the start of the cache.
    surface_cache = {}

    #: `dict` containing functions that return a :class:`~cairo.Surface` given a :class:`~cairo.Format`,
    #: width `int` and height `int`, see :meth:`~Gtk.Window.create_similar_image_surface`
    surface_factory = {}

    #: Size of the different managed widgets, as a `dict` of tuples
    surface_size = {}

    #: Type of document handled by each widget. It is a `dict`: its keys are
    #: widget names and its values are document types from ui.
    surface_type = {}

    #: Dictionary of :class:`~threading.Lock` used for managing conccurent
    #: accesses to :attr:`surface_cache` and :attr:`surface_size`
    locks = {}

    #: Set of widgets for which we ignore the max
    unlimited = set()

    #: The current :class:`~pympress.document.Document`.
    doc = None

    #: :class:`~threading.Lock` used to manage conccurent accesses to :attr:`doc`.
    doc_lock = None

    #: Set of active widgets
    active_widgets = set()

    #: `dict` of the managed :class:`~Gtk.Widget`, used to get their scale factor
    widgets = {}

    #: :class:`~collections.deque` of (widget name, PDF page number, scale) tuples to render in the background
    jobs = None

    #: :class:`~threading.Condition` protecting :attr:`jobs`, :attr:`generation` and :attr:`render_uri`
    jobs_cond = None

    #: `int` incremented when the document changes, to discard renders of the previous document
    generation = 0

    #: `str` URI of the document that the background thread renders
    render_uri = None

    #: maximum number of pages we keep in cache
    max_pages = 200

    def __init__(self, doc, max_pages):
        self.max_pages = max_pages
        self.doc = doc
        self.doc_lock = threading.Lock()
        self.jobs = collections.deque()
        self.jobs_cond = threading.Condition()
        self.render_uri = doc.get_uri()

        threading.Thread(target=self._render_worker, name='prerender', daemon=True).start()


    def add_widget(self, widget, wtype, prerender_enabled = True, zoomed = False, ignore_max = False):
        """ Add a widget to the list of widgets that have to be managed (for caching and prerendering).

        This creates new entries for ``widget_name`` in the needed internal data
        structures, and creates a new thread for prerendering pages for this widget.

        Args:
            widget (:class:`~Gtk.Widget`):  The widget for which we need to cache
            wtype (`int`):  type of document handled by the widget (see :attr:`surface_type`)
            prerender_enabled (`bool`):  whether this widget is initially in the list of widgets to prerender
            zoomed (`bool`): whether we will cache a zoomed portion of the widget
            ignore_max (`bool`): whether we will cache an unlimited number of slides
        """
        widget_name = widget.get_name().rstrip('0123456789') + ('_zoomed' if zoomed else '')
        with self.locks.setdefault(widget_name, threading.Lock()):
            self.surface_cache[widget_name] = collections.OrderedDict()
            self.surface_size[widget_name] = (-1, -1)
            self.surface_type[widget_name] = wtype
            self.surface_factory[widget_name] = functools.partial(self._create_surface, widget)
            self.widgets[widget_name] = widget
            if prerender_enabled and not zoomed:
                self.enable_prerender(widget_name)
            if ignore_max:
                self.unlimited.add(widget_name)


    def swap_document(self, new_doc):
        """ Replaces the current document for which to cache slides with a new one.

        This function also clears the cached pages, since they now belong to an outdated document.

        Args:
            new_doc (:class:`~pympress.document.Document`):  the new document
        """
        with self.doc_lock:
            self.doc = new_doc

        with self.jobs_cond:
            self.jobs.clear()
            self.generation += 1
            self.render_uri = new_doc.get_uri()

        self.clear_cache()


    def disable_prerender(self, widget_name):
        """ Remove a widget from the ones to be prerendered.

        Args:
            widget_name (`str`):  string used to identify a widget
        """
        self.active_widgets.discard(widget_name)


    def enable_prerender(self, widget_name):
        """ Add a widget to the ones to be prerendered.

        Args:
            widget_name (`str`):  string used to identify a widget
        """
        self.active_widgets.add(widget_name)


    def set_widget_type(self, widget_name, wtype):
        """ Set the document type of a widget.

        Args:
            widget_name (`str`):  string used to identify a widget
            wtype (`int`):  type of document handled by the widget (see :attr:`surface_type`)
        """
        with self.locks[widget_name]:
            if self.surface_type[widget_name] != wtype:
                self.surface_type[widget_name] = wtype
                self.surface_cache[widget_name].clear()


    def get_widget_type(self, widget_name):
        """ Get the document type of a widget.

        Args:
            widget_name (`str`):  string used to identify a widget

        Returns:
            `int`: type of document handled by the widget (see :attr:`surface_type`)
        """
        return self.surface_type[widget_name]


    def clear_cache(self, widget_name=None):
        """ Remove all cached values for a given widget. Useful for zoomed views.

        Args:
            widget_name (`str`):  name of the widget that is resized, `None` for all widgets.
        """
        for widget in [widget_name] if widget_name is not None else self.locks:
            with self.locks[widget]:
                self.surface_cache[widget].clear()


    def resize_widget(self, widget_name, width, height):
        """ Change the size of a registered widget, thus invalidating all the cached pages.

        Args:
            widget_name (`str`):  name of the widget that is resized
            width (`int`):  new width of the widget
            height (`int`):  new height of the widget
        """
        with self.locks[widget_name]:
            if (width, height) != self.surface_size[widget_name]:
                self.surface_cache[widget_name].clear()
                self.surface_size[widget_name] = (width, height)


    def get(self, widget_name, page_nb):
        """ Fetch a cached, prerendered page for the specified widget.

        Args:
            widget_name (`str`):  name of the concerned widget
            page_nb (`int`):  number of the page to fetch in the cache

        Returns:
            :class:`~cairo.ImageSurface`: the cached page if available, or `None` otherwise
        """
        with self.locks[widget_name]:
            pc = self.surface_cache[widget_name]
            if page_nb in pc:
                pc.move_to_end(page_nb)
                return pc[page_nb]
            else:
                return None


    def put(self, widget_name, page_nb, val):
        """ Store a rendered page in the cache.

        Args:
            widget_name (`str`):  name of the concerned widget
            page_nb (`int`):  number of the page to store in the cache
            val (:class:`~cairo.ImageSurface`):  content to store in the cache
        """
        with self.locks[widget_name]:
            pc = self.surface_cache[widget_name]
            pc[page_nb] = val
            pc.move_to_end(page_nb)

            while len(pc) > self.max_pages:
                pc.popitem(False)


    def _create_surface(self, widget, fmt, width, height):
        """ Given a widget, create a cairo Image surface with appropriate size and scaling.

        Args:
            widget (:class:`~Gtk.Widget`): the widget for which we’re caching data.
            fmt (:class:`~cairo.Format`): the format for the new surface
            width (`int`): width of the new surface
            height (`int`): height of the new surface

        Returns:
            :class:`~cairo.ImageSurface`: a new image surface
        """
        window = widget.get_window()
        scale = window.get_scale_factor()
        try:
            return window.create_similar_image_surface(fmt, width * scale, height * scale, scale)
        except cairo.Error:
            logger.warning('Failed creating a {} cache surface sized {}x{} scale {} for widget {}'
                           .format(fmt, width * scale, height * scale, scale, widget.get_name()), exc_info=True)
            # Warn here but handle error at the call site
            raise


    def prerender(self, page_nbs):
        """ Replace the pages queued for prerendering in the background.

        The specified pages will be prerendered for all the active widgets.

        Args:
            page_nbs (`list` of `int`):  numbers of the pages to be prerendered, by decreasing priority
        """
        jobs = []
        for page_nb in page_nbs:
            page = self.doc.page(page_nb)
            if page is None or not page.can_render():
                continue
            # Use PDF page numbering, so that the worker does not need the notes mapping
            jobs.extend((name, page.page_nb, self.widgets[name].get_scale_factor()) for name in self.active_widgets)

        with self.jobs_cond:
            self.jobs.clear()
            self.jobs.extend(jobs)
            self.jobs_cond.notify()


    def _render_worker(self):
        """ Background thread rendering the queued pages, using a Poppler document of its own.
        """
        poppler_doc, generation = None, -1
        while True:
            with self.jobs_cond:
                while not self.jobs:
                    self.jobs_cond.wait()
                widget_name, page_nb, scale = self.jobs.popleft()
                if generation != self.generation:
                    generation, uri, poppler_doc = self.generation, self.render_uri, None

            try:
                if poppler_doc is None and uri is not None:
                    poppler_doc = Poppler.Document.new_from_file(uri, None)
                self._render_job(poppler_doc, generation, widget_name, page_nb, scale)
            except Exception:
                logger.exception('Failed prerendering page {} for widget {}'.format(page_nb, widget_name))


    def _render_job(self, poppler_doc, generation, widget_name, page_nb, scale):
        """ Render a page for a widget and store it in the cache, unless the configuration changed in the meantime.

        Args:
            poppler_doc (:class:`~Poppler.Document`):  the document opened by the background thread
            generation (`int`):  the value of :attr:`generation` when the document was opened
            widget_name (`str`):  name of the concerned widget
            page_nb (`int`):  PDF number of the page to render
            scale (`int`):  scale factor of the widget
        """
        with self.locks[widget_name]:
            if page_nb in self.surface_cache[widget_name]:
                return
            ww, wh = self.surface_size[widget_name]
            wtype = self.surface_type[widget_name]

        if ww < 0 or wh < 0 or poppler_doc is None:
            return

        surface = cairo.ImageSurface(cairo.Format.RGB24, ww * scale, wh * scale)
        surface.set_device_scale(scale, scale)
        context = cairo.Context(surface)
        document.render_poppler_page(poppler_doc.get_page(page_nb), context, ww, wh, wtype)
        del context

        with self.locks[widget_name]:
            pc = self.surface_cache[widget_name]
            if generation != self.generation or page_nb in pc or \
                    (ww, wh) != self.surface_size[widget_name] or wtype != self.surface_type[widget_name]:
                return

            pc[page_nb] = surface
            if widget_name not in self.unlimited:
                while len(pc) > self.max_pages:
                    pc.popitem(False)


    def renderer(self, widget_name, page_nb):
        """ Rendering function.

        This function is meant to be scheduled on the GLib main loop. When run,
        it will go through the following steps:

        - check if the job's result is not already available in the cache
        - render it in a new :class:`~cairo.ImageSurface` if necessary
        - store it in the cache if it was not added there since the beginning of
          the process and the widget configuration is still valid

        Args:
            widget_name (`str`):  name of the concerned widget
            page_nb (`int`):  number of the page to store in the cache
        """
        with self.locks[widget_name]:
            if page_nb in self.surface_cache[widget_name]:
                # Already in cache
                return GLib.SOURCE_REMOVE
            ww, wh = self.surface_size[widget_name]
            wtype = self.surface_type[widget_name]

        if ww < 0 or wh < 0:
            logger.warning('Widget {} with invalid size {}x{} when rendering'.format(widget_name, ww, wh))
            return GLib.SOURCE_REMOVE

        with self.doc_lock:
            page = self.doc.page(page_nb)
            if page is None:
                return GLib.SOURCE_REMOVE

        # Render to a ImageSurface
        try:
            surface = self.surface_factory[widget_name](cairo.Format.RGB24, ww, wh)
        except AttributeError:
            logger.warning('Widget {} was not mapped when rendering'.format(widget_name), exc_info = True)
            return GLib.SOURCE_REMOVE
        except cairo.Error:
            return GLib.SOURCE_REMOVE

        context = cairo.Context(surface)
        page.render_cairo(context, ww, wh, wtype)
        del context

        # Save if possible and necessary − using PDF page numbering
        page_nb = page.page_nb
        with self.locks[widget_name]:
            pc = self.surface_cache[widget_name]
            if (ww, wh) == self.surface_size[widget_name] and page_nb not in pc:
                pc[page_nb] = surface

            if widget_name not in self.unlimited:
                pc.move_to_end(page_nb)
                while len(pc) > self.max_pages:
                    pc.popitem(False)

        return GLib.SOURCE_REMOVE


##
# Local Variables:
# mode: python
# indent-tabs-mode: nil
# py-indent-offset: 4
# fill-column: 80
# end:

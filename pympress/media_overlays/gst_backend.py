# -*- coding: utf-8 -*-
#
#       media_overlays/gst.py
#
#       Copyright 2018 Cimbali <me@cimba.li>
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
#       Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston MA 02110-1301, USA.
#
"""
:mod:`pympress.media_overlays.gst` -- widget to play videos using Gstreamer's Gst
---------------------------------------------------------------------------------------
"""

import logging
logger = logging.getLogger(__name__)

import gi
gi.require_version('Gst', '1.0')
gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, GLib, Gst, Gtk


from pympress import util
from pympress.media_overlays import base


def link_branch(tee, branch):
    """ Add a branch to the bin of a tee, fed by a new pad of the tee.

    Args:
        tee (:class:`~Gst.Element`): the tee splitting the decoded video towards each window
        branch (:class:`~Gst.Bin`): the branch to add
    """
    tee.get_parent().add(branch)
    request_pad = getattr(tee, 'request_pad_simple', None) or tee.get_request_pad
    request_pad('src_%u').link(branch.get_static_pad('sink'))
    # In case the pipeline is already set up
    branch.sync_state_with_parent()


def unlink_branch(tee, branch):
    """ Stop a branch and remove it from the bin of a tee, releasing the pad of the tee that fed it.

    Args:
        tee (:class:`~Gst.Element`): the tee splitting the decoded video towards each window
        branch (:class:`~Gst.Bin`): the branch to remove
    """
    branch.set_state(Gst.State.NULL)
    request_pad = branch.get_static_pad('sink').get_peer()
    request_pad.unlink(branch.get_static_pad('sink'))
    tee.release_request_pad(request_pad)
    tee.get_parent().remove(branch)


def make_branch(elements):
    """ Make a bin linking the given elements after a queue, to be fed by a tee.

    Args:
        elements (`list` of :class:`~Gst.Element`): the elements to link, ending with a sink

    Returns:
        :class:`~Gst.Bin`: the bin with a sink pad
    """
    branch = WindowBranch()
    elements = [Gst.ElementFactory.make('queue', None)] + elements
    for element in elements:
        branch.add(element)
    # Skip checking caps compatibility, which is slow with GL elements
    for src, dst in zip(elements, elements[1:]):
        src.get_static_pad('src').link_full(dst.get_static_pad('sink'), Gst.PadLinkCheck.HIERARCHY)
    branch.add_pad(Gst.GhostPad.new('sink', elements[0].get_static_pad('sink')))
    return branch


class WindowBranch(Gst.Bin):
    """ A bin for the part of the pipeline displaying the video in one window.

    Each window renders with its own OpenGL context, so GL contexts shared throughout the pipeline are ignored.
    Elements then query them from their own sink.
    """
    def do_set_context(self, context):
        """ Pass on contexts to the children, except OpenGL ones.

        Args:
            context (:class:`~Gst.Context`): the context shared through the pipeline
        """
        if not context.get_context_type().startswith('gst.gl.'):
            Gst.Bin.do_set_context(self, context)


class WindowSink(object):
    """ An OpenGL video sink for a window, shared by all the videos displayed in that window.

    Creating a GL sink is slow and finalizing one breaks the OpenGL rendering of GTK, so each window keeps one.
    It is lent to one video at a time, and only joins or leaves its pipeline while that pipeline is stopped.
    Its widget floats over the window, placed where the video is and just below its controls.

    Args:
        window (:class:`~Gtk.Overlay`): The container with the slide, over which videos are displayed
    """
    #: `dict` mapping each :class:`~Gtk.Overlay` window to its :class:`~WindowSink`
    windows = {}
    #: `bool` whether to try rendering videos with OpenGL, which is disabled once it fails
    enabled = True
    #: `list` of :class:`~WindowSink` that failed to start, kept as finalizing them breaks the rendering of GTK
    failed = []

    #: The :class:`~Gtk.Overlay` window in which the video is displayed
    window = None
    #: A :class:`~Gst.Bin` with the elements uploading and displaying the frames
    branch = None
    #: The gtkglsink :class:`~Gst.Element`
    sink = None
    #: The :class:`~Gtk.GLArea` widget of the :attr:`sink`
    widget = None
    #: The :class:`~pympress.media_overlays.gst_backend.GstOverlay` whose pipeline currently contains the sink
    owner = None
    #: `list` of the :class:`~pympress.media_overlays.gst_backend.GstOverlay` pipeline owners waiting to preroll
    waiting = []
    #: `bool` whether the sink started, i.e. OpenGL works
    started = False

    def __init__(self, window):
        self.window = window
        self.waiting = []
        self.sink = Gst.ElementFactory.make('gtkglsink', None)
        self.sink.set_property('ignore-alpha', False)
        self.widget = self.sink.props.widget
        self.widget.set_has_alpha(True)
        self.widget.set_no_show_all(True)
        self.window.add_overlay(self.widget)
        self.window.connect('get-child-position', self.place)

        # Create the GL context now on the main thread, as streaming threads would wait for the main loop to do it
        self.started = self.sink.set_state(Gst.State.READY) == Gst.StateChangeReturn.SUCCESS
        self.branch = make_branch([Gst.ElementFactory.make('glupload', None),
                                   Gst.ElementFactory.make('glcolorconvert', None), self.sink])


    @classmethod
    def get(cls, window):
        """ Get the GL sink of a window, creating it if needed.

        Args:
            window (:class:`~Gtk.Overlay`): The container with the slide, over which videos are displayed

        Returns:
            :class:`~WindowSink`: the sink, or `None` if OpenGL is not available
        """
        elements = ['glupload', 'glcolorconvert', 'gtkglsink']
        if cls.enabled and window not in cls.windows and all(Gst.ElementFactory.find(name) for name in elements):
            window_sink = cls(window)
            if window_sink.started:
                cls.windows[window] = window_sink
            else:
                logger.warning('Can not render videos with OpenGL, falling back to slower rendering')
                cls.enabled = False
                cls.failed.append(window_sink)
        return cls.windows.get(window) if cls.enabled else None


    def place(self, overlay, widget, allocation):
        """ Position the widget where the video of its owner is, connected to `get-child-position`.

        Args:
            overlay (:class:`~Gtk.Overlay`): the window positioning its children
            widget (:class:`~Gtk.Widget`): the child to position
            allocation (:class:`~Gdk.Rectangle`): the position and size to fill in

        Returns:
            `bool`: `True` iff the widget is ours and its position was set
        """
        if widget is not self.widget or self.owner is None:
            return False
        return self.owner.place(overlay, self.owner.media_overlay, allocation)


    def show_below(self, media_overlay):
        """ Show the widget, just below the controls of the video in the window.

        Args:
            media_overlay (:class:`~Gtk.Widget`): the overlay child containing the controls of the video
        """
        position = self.window.child_get_property(media_overlay, 'index')
        if self.window.child_get_property(self.widget, 'index') < position:
            position -= 1
        self.window.reorder_overlay(self.widget, position)
        self.widget.show()


    def attach(self, owner):
        """ Add the sink to the pipeline of a video, which must be stopped.

        Args:
            owner (:class:`~pympress.media_overlays.gst_backend.GstOverlay`): the overlay displaying the video
        """
        self.owner = owner
        link_branch(owner.tee, self.branch)
        self.window.queue_resize()


    def detach(self):
        """ Remove the sink from the pipeline of its owner, which must be stopped, and wake up the pipeline waiting.
        """
        self.widget.hide()
        unlink_branch(self.owner.tee, self.branch)
        self.owner = None

        for overlay in self.waiting:
            GLib.idle_add(overlay.resume_prepare)
        self.waiting.clear()


class GstOverlay(base.VideoOverlay):
    """ Simple Gstramer widget.

    Wraps a simple gstreamer playbin.
    """

    #: A :class:`~Gst.Playbin` to be play videos
    playbin = None
    #: A :class:`~Gst.Base.Sink` to display video content, while the pipeline has one for our window
    sink = None
    #: The :class:`~WindowSink` of our window, while our pipeline is using it
    window_sink = None
    #: A :class:`~Gtk.Box` containing the widget of the :attr:`sink`, with the controls floating over it
    video_box = None
    #: A :class:`~Gtk.Widget` in the :attr:`video_box` that stands in for the video displayed by the window sink
    placeholder = None
    #: The :class:`~Gtk.Widget` displaying the video, or the :attr:`placeholder`, whose visibility shows the video
    video_widget = None
    #: `list` of the :class:`~pympress.media_overlays.gst_backend.GstOverlay` displaying our pipeline, if we own it
    mirrors = []
    #: `bool` whether we wait for the sink of a window to be free before prerolling
    waiting_for_sink = False
    #: A :class:`~Gst.Element` tee splitting the decoded video towards the sink of each window
    tee = None
    #: The :class:`~pympress.media_overlays.gst_backend.GstOverlay` owning the pipeline we display, if not ourselves
    master = None
    #: `bool` whether to stop the pipeline once it is done prerolling, as stopping it meanwhile blocks
    stop_when_ready = False
    #: `list` of the handler ids of our signals on the bus of the pipeline
    bus_handlers = []
    #: `bool` whether to release the pipeline once it is done prerolling, as the overlay was removed meanwhile
    release_when_ready = False

    #: `int` number of milliseconds between updates
    update_freq = 200

    def __init__(self, *args, master=None, **kwargs):
        if master is None:
            # Create GStreamer playbin, with a tee as video sink so other windows can tap into the decoded stream
            self.playbin = Gst.ElementFactory.make('playbin', None)
            self.tee = Gst.ElementFactory.make('tee', None)
            video_sink = Gst.Bin.new(None)
            video_sink.add(self.tee)
            video_sink.add_pad(Gst.GhostPad.new('sink', self.tee.get_static_pad('sink')))
            self.playbin.set_property('video-sink', video_sink)
        else:
            self.playbin = master.playbin
            self.tee = master.tee
        self.master = master
        self.bus_handlers = []
        self.mirrors = []
        if master is not None:
            master.mirrors.append(self)

        super(GstOverlay, self).__init__(*args, **kwargs)

        # Float the controls over the video, so that they do not shrink it
        self.media_overlay.remove(self.movie_zone)
        self.media_overlay.remove(self.toolbar)
        video = Gtk.Overlay()
        self.video_box = Gtk.Box()
        video.add(self.video_box)
        video.add_overlay(self.toolbar)
        self.toolbar.set_valign(Gtk.Align.END)
        # Not show_all, which would show the controls even when the media does not want them
        video.show()
        self.video_box.show()
        # Pause or resume by clicking the video, while the controls handle their own clicks
        clickable = Gtk.EventBox(visible_window=False)
        clickable.add(video)
        clickable.connect('button-press-event', self.play_pause)
        clickable.show()
        self.media_overlay.pack_start(clickable, True, True, 0)

        # The widget of the window sink floats over the slide, and follows where the placeholder is shown
        self.placeholder = Gtk.DrawingArea()
        # Receive clicks, so that they reach the handler pausing the video
        self.placeholder.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.placeholder.connect('map', self.on_placeholder_mapped)
        self.placeholder.connect('unmap', self.on_placeholder_mapped)
        self.video_box.pack_start(self.placeholder, True, True, 0)
        self.video_widget = self.placeholder

        # Create bus to get events from GStreamer playin
        bus = self.playbin.get_bus()
        bus.add_signal_watch()
        bus.enable_sync_message_emission()
        # Only the pipeline owner handles ending, as the actions reach both overlays
        if master is None:
            self.bus_handlers.append(bus.connect('message::eos', lambda *args: GLib.idle_add(self.handle_end)))
            self.bus_handlers.append(bus.connect('message::error', self.on_error))
            self.bus_handlers.append(bus.connect('message::async-done', self.on_async_done))
        self.bus_handlers.append(bus.connect('message::state-changed', self.on_state_changed))
        self.bus_handlers.append(bus.connect('message::duration-changed',
                                             lambda *args: GLib.idle_add(self.do_update_duration)))


    @classmethod
    def mirror(cls, master, *args, **kwargs):
        """ Create an overlay showing the same media as another, decoding it only once.

        Args:
            master (:class:`~pympress.media_overlays.gst_backend.GstOverlay`): The overlay owning the pipeline
        """
        return cls(*args, master=master, **kwargs)


    def on_placeholder_mapped(self, placeholder):
        """ Show or hide the widget of the window sink where the video should appear or disappear.

        Args:
            placeholder (:class:`~Gtk.Widget`): the placeholder that got mapped or unmapped
        """
        if self.window_sink is None:
            return
        if placeholder.get_mapped():
            self.window_sink.show_below(self.media_overlay)
        else:
            self.window_sink.widget.hide()


    def attach_sink(self, may_wait):
        """ Add a sink displaying the video in our window to the pipeline, which must be stopped.

        The sink of the window is used if it is free, otherwise we get a gtksink of our own,
        e.g. for a second video on the same slide, unless we may wait for the window sink to be released.

        Args:
            may_wait (`bool`): whether to wait for the window sink if it is about to be released

        Returns:
            `bool`: `False` iff we are waiting for the window sink
        """
        if self.sink is not None:
            return True

        window_sink = WindowSink.get(self.parent)
        if window_sink is not None and window_sink.owner is None:
            window_sink.attach(self)
            self.window_sink = window_sink
            self.sink = window_sink.sink
            self.video_widget = self.placeholder
            return True

        if window_sink is not None and may_wait and window_sink.owner.is_stopping():
            window_sink.waiting.append(self.master or self)
            return False

        self.sink = Gst.ElementFactory.make('gtksink', None)
        # Leave the borders transparent when the video does not fill the media area, showing the poster
        self.sink.set_property('ignore-alpha', False)
        self.video_widget = self.sink.props.widget
        self.video_box.pack_start(self.video_widget, True, True, 0)
        link_branch(self.tee, make_branch([self.sink]))
        return True


    def detach_sink(self):
        """ Remove our sink from the pipeline, which must be stopped.
        """
        if self.window_sink is not None:
            self.window_sink.detach()
            self.window_sink = None
        elif self.sink is not None:
            unlink_branch(self.tee, self.sink.get_parent())
            if self.video_widget.get_parent() is not None:
                self.video_box.remove(self.video_widget)
        self.sink = None
        self.video_widget = self.placeholder


    def attach_sinks(self, may_wait=False):
        """ Add a sink for each window to our pipeline, which must be stopped.

        Args:
            may_wait (`bool`): whether to wait for window sinks that are about to be released

        Returns:
            `bool`: `True` iff all windows have a sink, otherwise we are waiting for one
        """
        attached = [overlay.attach_sink(may_wait) for overlay in [self] + self.mirrors]
        self.waiting_for_sink = not all(attached)
        return not self.waiting_for_sink


    def stop_pipeline(self):
        """ Stop our pipeline and remove its sinks, so that other videos can use the window sinks.
        """
        self.playbin.set_state(Gst.State.NULL)
        for overlay in [self] + self.mirrors:
            overlay.detach_sink()


    def is_stopping(self):
        """ Returns whether the pipeline displayed by this overlay is about to stop.

        Returns:
            `bool`: `True` iff the pipeline will stop once done prerolling
        """
        master = self.master or self
        return master.stop_when_ready or master.release_when_ready


    def resume_prepare(self):
        """ Preroll now that a window sink was released, if we still need to.

        Returns:
            `bool`: `True` iff this function should be run again (:func:`~GLib.idle_add` convention)
        """
        if self.waiting_for_sink and not self.removed:
            self.do_prepare()
        return False


    def remove(self):
        """ Stop and remove the widget from the overlays, then release the pipeline so that it can be freed.
        """
        super(GstOverlay, self).remove()
        # Stopping during preroll is deferred, and so is releasing the pipeline
        if self.stop_when_ready:
            self.release_when_ready = True
        else:
            self.release()


    def release(self):
        """ Stop the pipeline and our handlers on its bus, which keep this overlay alive.
        """
        if self.master is None:
            self.stop_pipeline()
            for window_sink in WindowSink.windows.values():
                if self in window_sink.waiting:
                    window_sink.waiting.remove(self)

        bus = self.playbin.get_bus()
        for handler_id in self.bus_handlers:
            bus.disconnect(handler_id)
        self.bus_handlers.clear()
        bus.disable_sync_message_emission()
        bus.remove_signal_watch()


    def is_playing(self):
        """ Returns whether the media is currently playing (and not paused).

        Returns:
            `bool`: `True` iff the media is playing.
        """
        return self.playbin.get_state(0).state == Gst.State.PLAYING


    def _set_file(self, filepath):
        """ Sets the media file to be played by the widget.

        Args:
            filepath (`pathlib.Path`): The path to the media file path
        """
        if self.master is not None:
            return
        self.playbin.set_property('uri', filepath.as_uri())
        self.playbin.set_state(Gst.State.READY)


    def mute(self, value):
        """ Mutes or unmutes the player.

        Args:
            value (`bool`): `True` iff this player should be muted
        """
        if self.master is not None:
            return False
        flags = self.playbin.get_property('flags')
        # Fall back to the documented value if introspection fails,
        # see https://gstreamer.freedesktop.org/documentation/playback/playsink.html?gi-language=python#GstPlayFlags
        audio_flag = util.introspect_flag_value(type(flags), 'audio', 0x02)
        if value:
            flags = flags & ~audio_flag
        else:
            flags = flags | audio_flag
        self.playbin.set_property('flags', flags)
        return False


    def on_state_changed(self, bus, msg):
        """ Callback triggered by playbin state changes.

        Args:
            bus (:class:`~Gst.Bus`): the bus that we are connected to
            msg (:class:`~Gst.Message`): the "state-changed" message
        """
        if msg.src != self.playbin:
            # ignore the playbin's children
            return
        old, new, pending = msg.parse_state_changed()
        if old == Gst.State.READY and new == Gst.State.PAUSED:
            # the playbin goes from READY (= stopped) to PLAYING (via PAUSED)
            self.on_initial_play()


    def on_error(self, bus, msg):
        """ Callback triggered by pipeline errors, which end prerolling, and fall back to gtksink if OpenGL failed.

        Args:
            bus (:class:`~Gst.Bus`): the bus that we are connected to
            msg (:class:`~Gst.Message`): the "error" message
        """
        logger.error('{} {}'.format(*msg.parse_error()))
        gl_failed = any(overlay.window_sink is not None and msg.src.has_as_ancestor(overlay.window_sink.branch)
                        for overlay in [self] + self.mirrors)
        if gl_failed:
            logger.warning('Failed to render videos with OpenGL, falling back to slower rendering')
            WindowSink.enabled = False
        stopping = self.is_stopping()

        # The pipeline will not finish prerolling, so apply a deferred stop now
        self.on_async_done(bus, msg)

        if gl_failed and not stopping:
            self.stop_pipeline()
            if self.is_shown():
                self.do_play()
            else:
                self.do_prepare()


    def on_async_done(self, bus, msg):
        """ Callback triggered when the pipeline finished prerolling, to apply a stop requested meanwhile.

        Args:
            bus (:class:`~Gst.Bus`): the bus that we are connected to
            msg (:class:`~Gst.Message`): the "async-done" message
        """
        if self.stop_when_ready:
            self.stop_when_ready = False
            self.stop_pipeline()
            if self.release_when_ready:
                self.release()


    def on_initial_play(self):
        """ Set starting position, start scrollbar updates, unhide overlay. """
        # set starting position, if needed
        if self.start_pos:
            self.do_set_time(self.start_pos)
        # ensure the scroll bar is updated
        GLib.idle_add(self.do_update_duration)
        GLib.timeout_add(self.update_freq, self.do_update_time)
        # ensure the overlay is visible (if needed)
        if not self.media_type.startswith('audio'):
            self.video_widget.show()


    def do_update_duration(self, *args):
        """ Transmit the change of file duration to the UI to adjust the scroll bar.
        """
        changed, time_ns = self.playbin.query_duration(Gst.Format.TIME)
        self.update_range(max(0, time_ns) / 1e9)


    def do_update_time(self):
        """ Update the current position in the progress bar.

        Returns:
            `bool`: `True` iff this function should be run again (:func:`~GLib.timeout_add` convention)
        """
        changed, time_ns = self.playbin.query_position(Gst.Format.TIME)
        time = time_ns / 1e9
        self.update_progress(time)
        if self.master is None and self.last_timestamp <= self.end_pos <= time:
            self.handle_end()
        self.last_timestamp = time
        return self.playbin.get_state(0).state in {Gst.State.PLAYING, Gst.State.PAUSED}


    def do_prepare(self):
        """ Preroll the pipeline in the background, so that playing only has to start the clock.
        """
        if self.master is None:
            self.stop_when_ready = False
            if self.playbin.get_state(0).state < Gst.State.PAUSED and self.attach_sinks(may_wait=True):
                self.playbin.set_state(Gst.State.PAUSED)


    def do_play(self):
        """ Start playing the media file.

        Returns:
            `bool`: `True` iff this function should be run again (:func:`~GLib.idle_add` convention)
        """
        self.stop_when_ready = False
        if self.master is None:
            self.attach_sinks()
        self.playbin.set_state(Gst.State.PLAYING)

        return False


    def do_play_pause(self):
        """ Toggle pause mode of the media.

        Should run on the main thread to ensure we avoid reentrency problems.

        Returns:
            `bool`: `True` iff this function should be run again (:func:`~GLib.idle_add` convention)
        """
        # Toggling is done once, by the pipeline owner
        if self.master is None:
            self.stop_when_ready = False
            # Toggle from the pending state, so a pause during a state change is not lost
            ret, state, pending = self.playbin.get_state(0)
            target = state if pending == Gst.State.VOID_PENDING else pending
            if target != Gst.State.PLAYING:
                self.attach_sinks()
            self.playbin.set_state(Gst.State.PAUSED if target == Gst.State.PLAYING else Gst.State.PLAYING)

        return False


    def do_stop(self):
        """ Stops playing in the backend player.
        """
        if self.master is None:
            self.waiting_for_sink = False
            # Stopping during preroll waits for the decoders to be set up, which can take seconds
            if self.playbin.get_state(0).pending != Gst.State.VOID_PENDING:
                self.stop_when_ready = True
            else:
                self.stop_pipeline()
        self.video_widget.hide()

        return False


    def do_set_time(self, time):
        """ Set the player at time `~time`.

        Should run on the main thread to ensure we avoid reentrency problems.

        Args:
            time (`float`): the timestamp, in s

        Returns:
            `bool`: `True` iff this function should be run again (:func:`~GLib.idle_add` convention)
        """
        self.last_timestamp = time
        if self.master is None:
            self.playbin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, time * Gst.SECOND)
        return False


    @classmethod
    def setup_backend(cls, gst_opts = [], opengl = True):
        """ Prepare/check the Gst backend.

        Args:
            gst_opts (`list`): the arguments for initializing GStreamer
            opengl (`bool`): whether to render videos with OpenGL when it is available

        Returns:
            `str`: the version of Gst used by the backend
        """
        Gst.init(gst_opts)
        WindowSink.enabled = opengl

        if Gst.ElementFactory.make('gtksink', None) is None:
            logger.warning('Can not create a gtksink. Check the gtk plugin for GStreamer is installed.')
            logger.warning('See https://github.com/Cimbali/pympress/issues/240')
            raise ValueError('Can not create a gtksink.')

        return Gst.version_string()

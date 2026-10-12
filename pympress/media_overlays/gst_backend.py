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
from gi.repository import GLib, Gst, Gtk


from pympress import util
from pympress.media_overlays import base


class GstOverlay(base.VideoOverlay):
    """ Simple Gstramer widget.

    Wraps a simple gstreamer playbin.
    """

    #: A :class:`~Gst.Playbin` to be play videos
    playbin = None
    #: A :class:`~Gst.Base.Sink` to display video content
    sink = None
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
        self.sink = self.add_sink()

        super(GstOverlay, self).__init__(*args, **kwargs)

        # Float the controls over the video, so that they do not shrink it
        self.media_overlay.remove(self.movie_zone)
        self.media_overlay.remove(self.toolbar)
        video = Gtk.Overlay()
        video.add(self.sink.props.widget)
        video.add_overlay(self.toolbar)
        self.toolbar.set_valign(Gtk.Align.END)
        video.show()
        self.media_overlay.pack_start(video, True, True, 0)
        self.sink.props.widget.hide()

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


    def add_sink(self):
        """ Add a branch to the tee with a new gtksink.

        Returns:
            :class:`~Gst.Element`: the new sink
        """
        queue = Gst.ElementFactory.make('queue', None)
        sink = Gst.ElementFactory.make('gtksink', None)
        # Leave the borders transparent when the video does not fill the media area, showing the poster
        sink.set_property('ignore-alpha', False)
        video_sink = self.tee.get_parent()
        video_sink.add(queue)
        video_sink.add(sink)
        queue.link(sink)
        request_pad = getattr(self.tee, 'request_pad_simple', None) or self.tee.get_request_pad
        request_pad('src_%u').link(queue.get_static_pad('sink'))
        # In case the pipeline is already set up
        queue.sync_state_with_parent()
        sink.sync_state_with_parent()
        return sink


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
            self.playbin.set_state(Gst.State.NULL)

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
        """ Callback triggered by pipeline errors, after which a deferred stop must be applied as prerolling ends.

        Args:
            bus (:class:`~Gst.Bus`): the bus that we are connected to
            msg (:class:`~Gst.Message`): the "error" message
        """
        logger.error('{} {}'.format(*msg.parse_error()))
        self.on_async_done(bus, msg)


    def on_async_done(self, bus, msg):
        """ Callback triggered when the pipeline finished prerolling, to apply a stop requested meanwhile.

        Args:
            bus (:class:`~Gst.Bus`): the bus that we are connected to
            msg (:class:`~Gst.Message`): the "async-done" message
        """
        if self.stop_when_ready:
            self.stop_when_ready = False
            self.playbin.set_state(Gst.State.NULL)
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
            self.sink.props.widget.show()


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
            if self.playbin.get_state(0).state < Gst.State.PAUSED:
                self.playbin.set_state(Gst.State.PAUSED)


    def do_play(self):
        """ Start playing the media file.

        Returns:
            `bool`: `True` iff this function should be run again (:func:`~GLib.idle_add` convention)
        """
        self.stop_when_ready = False
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
            self.playbin.set_state(Gst.State.PAUSED if target == Gst.State.PLAYING else Gst.State.PLAYING)

        return False


    def do_stop(self):
        """ Stops playing in the backend player.
        """
        if self.master is None:
            # Stopping during preroll waits for the decoders to be set up, which can take seconds
            if self.playbin.get_state(0).pending != Gst.State.VOID_PENDING:
                self.stop_when_ready = True
            else:
                self.playbin.set_state(Gst.State.NULL)
        self.sink.props.widget.hide()

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
    def setup_backend(cls, gst_opts = []):
        """ Prepare/check the Gst backend.

        Returns:
            `str`: the version of Gst used by the backend
        """
        Gst.init(gst_opts)

        if Gst.ElementFactory.make('gtksink', None) is None:
            logger.warning('Can not create a gtksink. Check the gtk plugin for GStreamer is installed.')
            logger.warning('See https://github.com/Cimbali/pympress/issues/240')
            raise ValueError('Can not create a gtksink.')

        return Gst.version_string()

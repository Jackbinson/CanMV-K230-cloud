"""H.264 MP4 writer for the installed Yahboom CanMV 1.4.1 firmware.

The legacy encoder returns SPS/PPS as separate HEADER packets. They are codec
configuration, not pictures. Combine them with an IDR; never write them as an
independent MP4 sample. Release every acquired encoder stream, also on errors.
"""
import os
import uctypes
import media.vencoder as encoder_api
from media.mp4format import Mp4Container
from mpp.mp4_format import (kd_mp4_write_frame, kd_mp4_create, kd_mp4_create_track,
                            kd_mp4_destroy_tracks, kd_mp4_destroy)
from mpp.mp4_format_struct import (k_mp4_frame_data_s, k_mp4_config_s,
                                  k_mp4_track_info_s)
from mpp.common_struct import k_u64_ptr


class SafeEncoder(encoder_api.Encoder):
    """Retain native packet descriptors until ReleaseStream returns.

    The installed GetStream stores a pointer to a temporary bytearray. Keep it
    rooted on self across Python allocations and allow queued packet batches.
    """
    def GetStream(self, chn, stream, timeout=1000):
        status = encoder_api.k_venc_chn_status()
        if encoder_api.kd_mpi_venc_query_status(chn, status):
            raise OSError("Encoder status failed")
        count = max(1, status.cur_packs)
        if count > 256:
            raise OSError("Unexpected encoder packet count")
        self._pack_buffer = bytearray(
            uctypes.sizeof(encoder_api.venc_def.k_venc_pack_desc, uctypes.NATIVE) * count)
        self.output.pack_cnt = count
        self.output.pack = uctypes.addressof(self._pack_buffer)
        if encoder_api.kd_mpi_venc_get_stream(chn, self.output, timeout):
            self._pack_buffer = None
            return -1
        stream.pack_cnt = self.output.pack_cnt
        for name in ('data', 'phy_addr', 'data_size', 'stream_type', 'pts'):
            values = getattr(stream, name)
            if len(values) < stream.pack_cnt:
                values.extend([0] * (stream.pack_cnt-len(values)))
        mapped = 0
        try:
            for i in range(stream.pack_cnt):
                pack = self.output._pack[i]
                stream.data[i] = encoder_api.kd_mpi_sys_mmap(pack.phys_addr, pack.len)
                stream.data_size[i] = pack.len
                stream.stream_type[i] = pack.type
                stream.pts[i] = pack.pts
                stream.phy_addr[i] = pack.phys_addr
                mapped += 1
        except BaseException:
            for i in range(mapped):
                encoder_api.kd_mpi_sys_munmap(stream.data[i], stream.data_size[i])
            encoder_api.kd_mpi_venc_release_stream(chn, self.output)
            self._pack_buffer = None
            raise
        return 0

    def ReleaseStream(self, chn, stream):
        super().ReleaseStream(chn, stream)
        self._pack_buffer = None


class CleanMp4Container(Mp4Container):
    def __init__(self):
        super().__init__()
        self.venc = SafeEncoder()
        self.recording_open = False
        self.hardware_started = False
        self.reset_counts()

    def reset_counts(self):
        self.frames_written = 0
        self.header_packets = 0
        self.skipped_before_idr = 0
        self.first_video_pts = None
        self.last_video_pts = None
        self.max_pts_gap_us = 0
        self.headers = bytearray()
        self.audio_timestamp = 0

    def Create(self, cfg):
        super().Create(cfg)
        self.recording_open = True
        self.pic_width = cfg.muxerCfg.pic_width
        self.pic_height = cfg.muxerCfg.pic_height

    def Start(self):
        super().Start()
        self.hardware_started = True

    def open_next(self, filename):
        # Keep camera, encoder and media pools alive between clips. In this
        # firmware MediaManager.deinit() clears lists that init() cannot rebuild.
        if self.recording_open:
            raise RuntimeError("Previous MP4 is still open")
        self.reset_counts()
        cfg = k_mp4_config_s()
        cfg.config_type = self.MP4_CONFIG_TYPE_MUXER
        cfg.muxer_config.file_name[:] = filename.encode()
        cfg.muxer_config.fmp4_flag = 0
        handle = k_u64_ptr()
        if kd_mp4_create(handle, cfg):
            raise OSError("Create next MP4 failed")
        self.mp4_handle = handle.value
        self.recording_open = True
        video = k_mp4_track_info_s()
        video.track_type = self.MP4_STREAM_TYPE_VIDEO
        video.time_scale = 1000
        video.video_info.width = self.pic_width
        video.video_info.height = self.pic_height
        video.video_info.codec_id = self.video_payload_type
        track = k_u64_ptr()
        if kd_mp4_create_track(self.mp4_handle, track, video):
            raise OSError("Create next video track failed")
        self.mp4_video_track_handle = track.value
        audio = k_mp4_track_info_s()
        audio.track_type = self.MP4_STREAM_TYPE_AUDIO
        audio.time_scale = 1000
        audio.audio_info.channels = 1
        audio.audio_info.codec_id = self.audio_payload_type
        audio.audio_info.sample_rate = 8000
        audio.audio_info.bit_per_sample = 16
        if kd_mp4_create_track(self.mp4_handle, track, audio):
            raise OSError("Create next audio track failed")
        self.mp4_audio_track_handle = track.value

    def close_file(self):
        if self.recording_open:
            if kd_mp4_destroy_tracks(self.mp4_handle):
                raise OSError("Close MP4 tracks failed")
            if kd_mp4_destroy(self.mp4_handle):
                raise OSError("Close MP4 failed")
            self.recording_open = False
            self.mp4_handle = 0

    def idle(self):
        # Drain frames while waiting for KEY so the hardware cannot back up.
        os.exitpoint(os.EXITPOINT_ENABLE_SLEEP)
        try:
            if self.venc.GetStream(0, self.stream_data, timeout=1000) == 0:
                self.venc.ReleaseStream(0, self.stream_data)
            self.audio_stream.read(block=False)
        finally:
            os.exitpoint(os.EXITPOINT_ENABLE)

    def shutdown(self):
        os.exitpoint(os.EXITPOINT_ENABLE_SLEEP)
        self.close_file()
        if self.hardware_started:
            self.sensor.stop()
            self.link.destroy()
            self.venc.Stop(0)
            self.audio_stream.stop_stream()
            self.audio_stream.close()
            self.pyaudio.terminate()
            self.aenc.destroy()
            self.venc.Destroy(0)
            self.hardware_started = False
        # The interpreter frees media pools when this script ends. Do not
        # deinitialize them while Python will continue to run other clips.

    def Process(self):
        # The IDE raises a generic Exception("IDE interrupt") in this firmware.
        # Defer it until the stream has been returned to the encoder.
        os.exitpoint(os.EXITPOINT_ENABLE_SLEEP)
        acquired = False
        try:
            ret = self.venc.GetStream(0, self.stream_data, timeout=1000)
            if ret != 0:
                raise OSError("Encoder did not return a frame within 1000 ms")
            acquired = True
            for index in range(self.stream_data.pack_cnt):
                kind = self.stream_data.stream_type[index]
                size = self.stream_data.data_size[index]
                pointer = self.stream_data.data[index]
                if kind == self.venc.STREAM_TYPE_HEADER:
                    self.header_packets += 1
                    self.headers.extend(uctypes.bytearray_at(pointer, size))
                    if len(self.headers) > 65536:
                        raise ValueError("Unexpected H.264 header size")
                    continue
                if kind not in (self.venc.STREAM_TYPE_I, self.venc.STREAM_TYPE_P):
                    raise ValueError("Unsupported encoded packet type")
                pts = self.stream_data.pts[index]
                if self.first_video_pts is None:
                    if kind != self.venc.STREAM_TYPE_I:
                        self.skipped_before_idr += 1
                        continue
                    if not self.headers:
                        raise ValueError("First IDR has no SPS/PPS")
                    self.first_video_pts = pts
                if self.last_video_pts is not None and pts <= self.last_video_pts:
                    raise ValueError("Encoder frame timestamps are not increasing")
                frame = k_mp4_frame_data_s()
                frame.codec_id = self.video_payload_type
                frame.time_stamp = pts - self.first_video_pts
                payload = None
                if kind == self.venc.STREAM_TYPE_I and self.headers:
                    payload = self.headers + uctypes.bytearray_at(pointer, size)
                    frame.data = uctypes.addressof(payload)
                    frame.data_length = len(payload)
                    self.headers = bytearray()
                else:
                    frame.data = pointer
                    frame.data_length = size
                if kd_mp4_write_frame(self.mp4_handle, self.mp4_video_track_handle, frame):
                    raise OSError("MP4 video write failed")
                if self.last_video_pts is not None:
                    self.max_pts_gap_us = max(self.max_pts_gap_us, pts-self.last_video_pts)
                self.last_video_pts = pts
                self.frames_written += 1
            audio_data = self.audio_stream.read(block=False)
            if audio_data:
                encoded = self.aenc.encode(audio_data)
                frame = k_mp4_frame_data_s()
                frame.codec_id = self.audio_payload_type
                frame.data = uctypes.addressof(encoded)
                frame.data_length = len(encoded)
                frame.time_stamp = self.audio_timestamp
                if kd_mp4_write_frame(self.mp4_handle, self.mp4_audio_track_handle, frame):
                    raise OSError("MP4 audio write failed")
                self.audio_timestamp += 40000
        finally:
            try:
                if acquired:
                    self.venc.ReleaseStream(0, self.stream_data)
            finally:
                os.exitpoint(os.EXITPOINT_ENABLE)

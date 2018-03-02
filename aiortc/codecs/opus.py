import audioop

from ._opus import ffi, lib


class OpusEncoder:
    timestamp_increment = 960

    def __init__(self):
        error = ffi.new('int *')
        self.cdata = ffi.new('char []', 960)
        self.buffer = ffi.buffer(self.cdata)
        self.encoder = lib.opus_encoder_create(48000, 2, lib.OPUS_APPLICATION_VOIP, error)
        self.rate_state = None

    def __del__(self):
        lib.opus_encoder_destroy(self.encoder)

    def encode(self, frame):
        data = frame.data

        # resample at 48 kHz
        if frame.sample_rate != 48000:
            data, self.rate_state = audioop.ratecv(
                data,
                frame.sample_width,
                frame.channels,
                frame.sample_rate,
                48000,
                self.rate_state)

        # convert to stereo
        if frame.channels == 1:
            data = audioop.tostereo(data, frame.sample_width, 1, 1)

        length = lib.opus_encode(self.encoder, ffi.cast('int16_t*', ffi.from_buffer(data)),
                                 960, self.cdata, len(self.cdata))
        return self.buffer[0:length]

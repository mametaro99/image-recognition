import asyncio
import enum
import hmac
import logging
import math
import os
import time
from struct import pack, unpack

import attr
import crcmod.predefined
from pyee import EventEmitter

from .exceptions import InvalidStateError
from .rtcdatachannel import RTCDataChannel, RTCDataChannelParameters
from .utils import first_completed, random32

crc32c = crcmod.predefined.mkPredefinedCrcFun('crc-32c')
logger = logging.getLogger('sctp')

# local constants
COOKIE_LENGTH = 24
COOKIE_LIFETIME = 60
USERDATA_MAX_LENGTH = 1200

# protocol constants
SCTP_CAUSE_INVALID_STREAM = 0x0001
SCTP_CAUSE_STALE_COOKIE = 0x0003

SCTP_DATA_LAST_FRAG = 0x01
SCTP_DATA_FIRST_FRAG = 0x02
SCTP_DATA_UNORDERED = 0x04

SCTP_SEQ_MODULO = 2 ** 16
SCTP_TSN_MODULO = 2 ** 32

STATE_COOKIE = 0x0007

# data channel constants
DATA_CHANNEL_ACK = 2
DATA_CHANNEL_OPEN = 3

DATA_CHANNEL_RELIABLE = 0

WEBRTC_DCEP = 50
WEBRTC_STRING = 51
WEBRTC_BINARY = 53
WEBRTC_STRING_EMPTY = 56
WEBRTC_BINARY_EMPTY = 57


def decode_params(body):
    params = []
    pos = 0
    while pos <= len(body) - 4:
        param_type, param_length = unpack('!HH', body[pos:pos + 4])
        params.append((param_type, body[pos + 4:pos + param_length]))
        pos += param_length + padl(param_length)
    return params


def encode_params(params):
    body = b''
    padding = b''
    for param_type, param_value in params:
        param_length = len(param_value) + 4
        body += padding
        body += pack('!HH', param_type, param_length) + param_value
        padding = b'\x00' * padl(param_length)
    return body


def padl(l):
    return 4 * ((l + 3) // 4) - l


def swapl(i):
    return unpack("<I", pack(">I", i))[0]


def seq_gt(a, b):
    """
    Return True if seq a is greater than b.
    """
    half_mod = (1 << 15)
    return (((a < b) and ((b - a) > half_mod)) or
            ((a > b) and ((a - b) < half_mod)))


def seq_plus_one(a):
    return (a + 1) % SCTP_SEQ_MODULO


def tsn_gt(a, b):
    """
    Return True if tsn a is greater than b.
    """
    half_mod = (1 << 31)
    return (((a < b) and ((b - a) > half_mod)) or
            ((a > b) and ((a - b) < half_mod)))


def tsn_gte(a, b):
    """
    Return True if tsn a is greater than or equal to b.
    """
    return (a == b) or tsn_gt(a, b)


def tsn_minus_one(a):
    return (a - 1) % SCTP_TSN_MODULO


def tsn_plus_one(a):
    return (a + 1) % SCTP_TSN_MODULO


class Chunk:
    def __init__(self, flags=0, body=b''):
        self.flags = flags
        self.body = body

    def __bytes__(self):
        body = self.body
        data = pack('!BBH', self.type, self.flags, len(body) + 4) + body
        data += b'\x00' * padl(len(body))
        return data

    def __repr__(self):
        return '%s(flags=%d)' % (self.__class__.__name__, self.flags)

    @property
    def type(self):
        for k, cls in CHUNK_TYPES.items():
            if isinstance(self, cls):
                return k


class BaseParamsChunk(Chunk):
    def __init__(self, flags=0, body=b''):
        self.flags = flags
        if body:
            self.params = decode_params(body)
        else:
            self.params = []

    @property
    def body(self):
        return encode_params(self.params)


class AbortChunk(BaseParamsChunk):
    pass


class CookieAckChunk(Chunk):
    pass


class CookieEchoChunk(Chunk):
    pass


class DataChunk(Chunk):
    def __init__(self, flags=0, body=b''):
        self.flags = flags
        if body:
            (self.tsn, self.stream_id, self.stream_seq, self.protocol) = unpack('!LHHL', body[0:12])
            self.user_data = body[12:]
        else:
            self.tsn = 0
            self.stream_id = 0
            self.stream_seq = 0
            self.protocol = 0
            self.user_data = b''

    @property
    def body(self):
        body = pack('!LHHL', self.tsn, self.stream_id, self.stream_seq, self.protocol)
        body += self.user_data
        return body

    def __repr__(self):
        return 'DataChunk(flags=%d, tsn=%d, stream_id=%d, stream_seq=%d)' % (
            self.flags, self.tsn, self.stream_id, self.stream_seq)


class ErrorChunk(BaseParamsChunk):
    pass


class HeartbeatChunk(BaseParamsChunk):
    pass


class HeartbeatAckChunk(BaseParamsChunk):
    pass


class BaseInitChunk(Chunk):
    def __init__(self, flags=0, body=b''):
        self.flags = flags
        if body:
            (self.initiate_tag, self.advertised_rwnd, self.outbound_streams,
             self.inbound_streams, self.initial_tsn) = unpack('!LLHHL', body[0:16])
            self.params = decode_params(body[16:])
        else:
            self.initiate_tag = 0
            self.advertised_rwnd = 0
            self.outbound_streams = 0
            self.inbound_streams = 0
            self.initial_tsn = 0
            self.params = []

    @property
    def body(self):
        body = pack(
            '!LLHHL', self.initiate_tag, self.advertised_rwnd, self.outbound_streams,
            self.inbound_streams, self.initial_tsn)
        body += encode_params(self.params)
        return body


class InitChunk(BaseInitChunk):
    pass


class InitAckChunk(BaseInitChunk):
    pass


class SackChunk(Chunk):
    def __init__(self, flags=0, body=b''):
        self.flags = flags
        self.gaps = []
        self.duplicates = []
        if body:
            self.cumulative_tsn, self.advertised_rwnd, nb_gaps, nb_duplicates = unpack(
                '!LLHH', body[0:12])
            pos = 12
            for i in range(nb_gaps):
                self.gaps.append(unpack('!HH', body[pos:pos + 4]))
                pos += 4
            for i in range(nb_duplicates):
                self.duplicates.append(unpack('!L', body[pos:pos + 4])[0])
                pos += 4
        else:
            self.cumulative_tsn = 0
            self.advertised_rwnd = 0

    @property
    def body(self):
        body = pack('!LLHH', self.cumulative_tsn, self.advertised_rwnd,
                    len(self.gaps), len(self.duplicates))
        for gap in self.gaps:
            body += pack('!HH', *gap)
        for tsn in self.duplicates:
            body += pack('!L', tsn)
        return body

    def __repr__(self):
        return 'SackChunk(flags=%d, advertised_rwnd=%d, cumulative_tsn=%d)' % (
            self.flags, self.advertised_rwnd, self.cumulative_tsn)


class ShutdownChunk(Chunk):
    def __init__(self, flags=0, body=b''):
        self.flags = flags
        if body:
            self.cumulative_tsn = unpack('!L', body[0:4])[0]
        else:
            self.cumulative_tsn = 0

    @property
    def body(self):
        return pack('!L', self.cumulative_tsn)

    def __repr__(self):
        return 'ShutdownChunk(flags=%d, cumulative_tsn=%d)' % (
            self.flags, self.cumulative_tsn)


class ShutdownAckChunk(Chunk):
    pass


class ShutdownCompleteChunk(Chunk):
    pass


CHUNK_TYPES = {
    0: DataChunk,
    1: InitChunk,
    2: InitAckChunk,
    3: SackChunk,
    4: HeartbeatChunk,
    5: HeartbeatAckChunk,
    6: AbortChunk,
    7: ShutdownChunk,
    8: ShutdownAckChunk,
    9: ErrorChunk,
    10: CookieEchoChunk,
    11: CookieAckChunk,
    14: ShutdownCompleteChunk,
}


class Packet:
    def __init__(self, source_port, destination_port, verification_tag):
        self.source_port = source_port
        self.destination_port = destination_port
        self.verification_tag = verification_tag
        self.chunks = []

    def __bytes__(self):
        checksum = 0
        data = pack(
            '!HHLL',
            self.source_port,
            self.destination_port,
            self.verification_tag,
            checksum)
        for chunk in self.chunks:
            data += bytes(chunk)

        # calculate checksum
        checksum = swapl(crc32c(data))
        return data[0:8] + pack('!L', checksum) + data[12:]

    @classmethod
    def parse(cls, data):
        if len(data) < 12:
            raise ValueError('SCTP packet length is less than 12 bytes')

        source_port, destination_port, verification_tag, checksum = unpack(
            '!HHLL', data[0:12])

        # verify checksum
        check_data = data[0:8] + b'\x00\x00\x00\x00' + data[12:]
        if checksum != swapl(crc32c(check_data)):
            raise ValueError('SCTP packet has invalid checksum')

        packet = cls(
            source_port=source_port,
            destination_port=destination_port,
            verification_tag=verification_tag)

        pos = 12
        while pos <= len(data) - 4:
            chunk_type, chunk_flags, chunk_length = unpack('!BBH', data[pos:pos + 4])
            chunk_body = data[pos + 4:pos + chunk_length]
            chunk_cls = CHUNK_TYPES.get(chunk_type)
            if chunk_cls:
                packet.chunks.append(chunk_cls(
                    flags=chunk_flags,
                    body=chunk_body))
            pos += chunk_length + padl(chunk_length)
        return packet


class InboundStream:
    def __init__(self):
        self.reassembly = []
        self.sequence_number = 0

    def add_chunk(self, chunk):
        pos = None
        for i, rchunk in enumerate(self.reassembly):
            if rchunk.tsn == chunk.tsn:
                return
            if tsn_gt(rchunk.tsn, chunk.tsn):
                pos = i
                break
        if pos is None:
            pos = len(self.reassembly)

        self.reassembly.insert(pos, chunk)

    def pop_messages(self):
        first_frag = True
        for pos, chunk in enumerate(self.reassembly[:]):
            if chunk.stream_seq != self.sequence_number:
                break

            if first_frag:
                if not (chunk.flags & SCTP_DATA_FIRST_FRAG):
                    break
                expected_tsn = chunk.tsn
                first_frag = False
                user_data = chunk.user_data
            else:
                if chunk.tsn != expected_tsn:
                    break
                user_data += chunk.user_data

            if (chunk.flags & SCTP_DATA_LAST_FRAG):
                self.reassembly = self.reassembly[pos + 1:]
                self.sequence_number = seq_plus_one(self.sequence_number)
                first_frag = True
                yield (chunk.stream_id, chunk.protocol, user_data)

            expected_tsn = tsn_plus_one(expected_tsn)


@attr.s
class RTCSctpCapabilities:
    """
    The :class:`RTCSctpCapabilities` dictionary provides information about the
    capabilities of the :class:`RTCSctpTransport`.
    """
    maxMessageSize = attr.ib()
    """
    The maximum size of data that the implementation can send or
    0 if the implementation can handle messages of any size.
    """


class RTCSctpTransport(EventEmitter):
    """
    The :class:`RTCSctpTransport` interface includes information relating to
    Stream Control Transmission Protocol (SCTP) transport.

    :param: transport: An :class:`RTCDtlstransport`.
    """
    def __init__(self, transport, port=5000):
        if transport.state == 'closed':
            raise InvalidStateError

        super().__init__()
        self.state = self.State.CLOSED
        self.__transport = transport
        self.closed = asyncio.Event()

        self.hmac_key = os.urandom(16)
        self.advertised_rwnd = 131072

        self.inbound_streams = 65535
        self._inbound_streams = {}

        self.outbound_streams = 65535
        self._outbound_stream_seq = {}

        self._sack_duplicates = []
        self._sack_needed = False

        self.__local_port = port
        self.local_tsn = random32()
        self.local_verification_tag = random32()

        self._last_received_tsn = None
        self._remote_port = None
        self._remote_verification_tag = 0

        # data channels
        self._data_channel_id = None
        self._data_channel_queue = []
        self._data_channels = {}

    @property
    def is_server(self):
        return self.transport.transport.role != 'controlling'

    @property
    def port(self):
        """
        The local SCTP port number used for data channels.
        """
        return self.__local_port

    @property
    def role(self):
        return self.is_server and 'server' or 'client'

    @property
    def transport(self):
        """
        The :class:`RTCDtlsTransport` over which SCTP data is transmitted.
        """
        return self.__transport

    def getCapabilities(self):
        """
        Retrieve the capabilities of the transport.

        :rtype: RTCSctpCapabilities
        """
        return RTCSctpCapabilities(maxMessageSize=65536)

    def start(self, remoteCaps, remotePort):
        """
        Start the transport.
        """
        self._remote_port = remotePort
        asyncio.ensure_future(self.__run())

    async def stop(self):
        """
        Stop the transport.
        """
        await self._shutdown()

    async def _abort(self):
        """
        Abort the association.
        """
        chunk = AbortChunk()
        await self._send_chunk(chunk)
        self._set_state(self.State.CLOSED)

    async def __run(self):
        # initialise local channel ID counter
        if self.is_server:
            self._data_channel_id = 0
        else:
            self._data_channel_id = 1

        if not self.is_server:
            chunk = InitChunk()
            chunk.initiate_tag = self.local_verification_tag
            chunk.advertised_rwnd = self.advertised_rwnd
            chunk.outbound_streams = self.outbound_streams
            chunk.inbound_streams = self.inbound_streams
            chunk.initial_tsn = self.local_tsn
            await self._send_chunk(chunk)
            self._set_state(self.State.COOKIE_WAIT)

        while True:
            try:
                data = await first_completed(self.transport.data.recv(), self.closed.wait())
            except ConnectionError:
                self.__log_debug('x Underlying connection closed while reading')
                self._set_state(self.State.CLOSED)
                break
            if data is True:
                break

            try:
                packet = Packet.parse(data)
            except ValueError:
                continue

            # is this an init?
            init_chunk = len([x for x in packet.chunks if isinstance(x, InitChunk)])
            if init_chunk:
                assert len(packet.chunks) == 1
                expected_tag = 0
            else:
                expected_tag = self.local_verification_tag

            # verify tag
            if packet.verification_tag != expected_tag:
                self.__log_debug('Bad verification tag %d vs %d',
                                 packet.verification_tag, expected_tag)
                continue

            # handle chunks
            for chunk in packet.chunks:
                await self._receive_chunk(chunk)

            # send SACK if needed
            if self._sack_needed:
                sack = SackChunk()
                sack.cumulative_tsn = self._last_received_tsn
                sack.advertised_rwnd = self.advertised_rwnd
                sack.duplicates = self._sack_duplicates
                await self._send_chunk(sack)

                self._sack_duplicates = []
                self._sack_needed = False

    def _get_timestamp(self):
        return int(time.time())

    async def _receive(self, stream_id, pp_id, data):
        """
        Receive data stream -> ULP.
        """
        await self._data_channel_receive(stream_id, pp_id, data)

    async def _receive_chunk(self, chunk):
        self.__log_debug('< %s', repr(chunk))

        # server
        if isinstance(chunk, InitChunk) and self.is_server:
            self._last_received_tsn = tsn_minus_one(chunk.initial_tsn)
            self._remote_verification_tag = chunk.initiate_tag

            ack = InitAckChunk()
            ack.initiate_tag = self.local_verification_tag
            ack.advertised_rwnd = self.advertised_rwnd
            ack.outbound_streams = self.outbound_streams
            ack.inbound_streams = self.inbound_streams
            ack.initial_tsn = self.local_tsn

            # generate state cookie
            cookie = pack('!L', self._get_timestamp())
            cookie += hmac.new(self.hmac_key, cookie, 'sha1').digest()
            ack.params.append((STATE_COOKIE, cookie))
            await self._send_chunk(ack)
        elif isinstance(chunk, CookieEchoChunk) and self.is_server:
            # check state cookie MAC
            cookie = chunk.body
            if (len(cookie) != COOKIE_LENGTH or
               hmac.new(self.hmac_key, cookie[0:4], 'sha1').digest() != cookie[4:]):
                self.__log_debug('x State cookie is invalid')
                return

            # check state cookie lifetime
            now = self._get_timestamp()
            stamp = unpack('!L', cookie[0:4])[0]
            if stamp < now - COOKIE_LIFETIME or stamp > now:
                self.__log_debug('x State cookie has expired')
                error = ErrorChunk()
                error.params.append((SCTP_CAUSE_STALE_COOKIE, b'\x00' * 8))
                await self._send_chunk(error)
                return

            ack = CookieAckChunk()
            await self._send_chunk(ack)
            self._set_state(self.State.ESTABLISHED)

        # client
        if isinstance(chunk, InitAckChunk) and not self.is_server:
            self._last_received_tsn = tsn_minus_one(chunk.initial_tsn)
            self._remote_verification_tag = chunk.initiate_tag

            echo = CookieEchoChunk()
            for k, v in chunk.params:
                if k == STATE_COOKIE:
                    echo.body = v
                    break
            await self._send_chunk(echo)
            self._set_state(self.State.COOKIE_ECHOED)
        elif isinstance(chunk, CookieAckChunk) and not self.is_server:
            self._set_state(self.State.ESTABLISHED)
        elif (isinstance(chunk, ErrorChunk) and not self.is_server and
              self.state in [self.State.COOKIE_WAIT, self.State.COOKIE_ECHOED]):
            self._set_state(self.State.CLOSED)
            self.__log_debug('x Could not establish association')
            return

        # common
        elif isinstance(chunk, DataChunk):
            if tsn_gte(self._last_received_tsn, chunk.tsn):
                # it's a duplicate
                self._sack_duplicates.append(chunk.tsn)
                self._sack_needed = True
                return
            elif chunk.tsn != tsn_plus_one(self._last_received_tsn):
                # it's out of order
                self._sack_needed = True
                return

            self._last_received_tsn = chunk.tsn
            self._sack_needed = True

            # FIXME: wrong place for initialization
            if chunk.stream_id not in self._inbound_streams:
                self._inbound_streams[chunk.stream_id] = InboundStream()
            inbound_stream = self._inbound_streams[chunk.stream_id]

            # defragment data
            inbound_stream.add_chunk(chunk)
            for message in inbound_stream.pop_messages():
                await self._receive(*message)

        elif isinstance(chunk, SackChunk):
            # TODO
            pass
        elif isinstance(chunk, HeartbeatChunk):
            ack = HeartbeatAckChunk()
            ack.params = chunk.params
            await self._send_chunk(ack)
        elif isinstance(chunk, AbortChunk):
            self.__log_debug('x Association was aborted by remote party')
            self._set_state(self.State.CLOSED)
        elif isinstance(chunk, ShutdownChunk):
            self._set_state(self.State.SHUTDOWN_RECEIVED)
            ack = ShutdownAckChunk()
            await self._send_chunk(ack)
            self._set_state(self.State.SHUTDOWN_ACK_SENT)
        elif isinstance(chunk, ShutdownAckChunk):
            complete = ShutdownCompleteChunk()
            await self._send_chunk(complete)
            self._set_state(self.State.CLOSED)
        elif isinstance(chunk, ShutdownCompleteChunk):
            self._set_state(self.State.CLOSED)

    async def _send(self, stream_id, pp_id, user_data):
        """
        Send data ULP -> stream.
        """
        stream_seq = self._outbound_stream_seq.get(stream_id, 0)

        fragments = math.ceil(len(user_data) / USERDATA_MAX_LENGTH)
        pos = 0
        for fragment in range(0, fragments):
            chunk = DataChunk()
            chunk.flags = 0
            if fragment == 0:
                chunk.flags |= SCTP_DATA_FIRST_FRAG
            if fragment == fragments - 1:
                chunk.flags |= SCTP_DATA_LAST_FRAG
            chunk.tsn = self.local_tsn
            chunk.stream_id = stream_id
            chunk.stream_seq = stream_seq
            chunk.protocol = pp_id
            chunk.user_data = user_data[pos:pos + USERDATA_MAX_LENGTH]

            pos += USERDATA_MAX_LENGTH
            self.local_tsn = tsn_plus_one(self.local_tsn)
            await self._send_chunk(chunk)

        self._outbound_stream_seq[stream_id] = seq_plus_one(stream_seq)

    async def _send_chunk(self, chunk):
        """
        Transmit a chunk (no bundling for now).
        """
        self.__log_debug('> %s', repr(chunk))
        packet = Packet(
            source_port=self.__local_port,
            destination_port=self._remote_port,
            verification_tag=self._remote_verification_tag)
        packet.chunks.append(chunk)
        await self.transport.data.send(bytes(packet))

    def _set_state(self, state):
        if state != self.state:
            self.__log_debug('- %s -> %s', self.state, state)
            self.state = state
            if state == self.State.ESTABLISHED:
                asyncio.ensure_future(self._data_channel_flush())
            elif state == self.State.CLOSED:
                self.closed.set()

    async def _shutdown(self):
        """
        Shutdown the association gracefully.
        """
        if self.state == self.State.CLOSED:
            self.closed.set()
            return

        chunk = ShutdownChunk()
        chunk.cumulative_tsn = self._last_received_tsn
        await self._send_chunk(chunk)
        self._set_state(self.State.SHUTDOWN_SENT)
        await self.closed.wait()

    async def _data_channel_flush(self):
        if self.state != self.State.ESTABLISHED:
            return

        for channel, protocol, user_data in self._data_channel_queue:
            # register channel if necessary
            stream_id = channel.id
            if stream_id is None:
                stream_id = self._data_channel_id
                self._data_channels[stream_id] = channel
                self._data_channel_id += 2
                channel._setId(stream_id)

            # send data
            await self._send(stream_id, protocol, user_data)

        self._data_channel_queue = []

    def _data_channel_open(self, channel):
        data = pack('!BBHLHH', DATA_CHANNEL_OPEN, DATA_CHANNEL_RELIABLE,
                    0, 0, len(channel.label), len(channel.protocol))
        data += channel.label.encode('utf8')
        data += channel.protocol.encode('utf8')
        self._data_channel_queue.append((channel, WEBRTC_DCEP, data))
        asyncio.ensure_future(self._data_channel_flush())

    async def _data_channel_receive(self, stream_id, pp_id, data):
        if pp_id == WEBRTC_DCEP and len(data):
            msg_type = unpack('!B', data[0:1])[0]
            if msg_type == DATA_CHANNEL_OPEN and len(data) >= 12:
                # we should not receive an open for an existing channel
                assert stream_id not in self._data_channels

                # one side should be using even IDs, the other odd IDs
                assert (stream_id % 2) != (self._data_channel_id % 2)

                (msg_type, channel_type, priority, reliability,
                 label_length, protocol_length) = unpack('!BBHLHH', data[0:12])
                pos = 12
                label = data[pos:pos + label_length].decode('utf8')
                pos += label_length
                protocol = data[pos:pos + protocol_length].decode('utf8')

                # register channel
                parameters = RTCDataChannelParameters(label=label, protocol=protocol)
                channel = RTCDataChannel(self, parameters, id=stream_id)
                channel._setReadyState('open')
                self._data_channels[stream_id] = channel

                # send ack
                self._data_channel_queue.append(
                    (channel, WEBRTC_DCEP, pack('!B', DATA_CHANNEL_ACK)))
                await self._data_channel_flush()

                # emit channel
                self.emit('datachannel', channel)
            elif msg_type == DATA_CHANNEL_ACK:
                assert stream_id in self._data_channels
                channel = self._data_channels[stream_id]
                channel._setReadyState('open')
        elif pp_id == WEBRTC_STRING and stream_id in self._data_channels:
            # emit message
            self._data_channels[stream_id].emit('message', data.decode('utf8'))
        elif pp_id == WEBRTC_STRING_EMPTY and stream_id in self._data_channels:
            # emit message
            self._data_channels[stream_id].emit('message', '')
        elif pp_id == WEBRTC_BINARY and stream_id in self._data_channels:
            # emit message
            self._data_channels[stream_id].emit('message', data)
        elif pp_id == WEBRTC_BINARY_EMPTY and stream_id in self._data_channels:
            # emit message
            self._data_channels[stream_id].emit('message', b'')

    async def _data_channel_send(self, channel, data):
        if data == '':
            pp_id, user_data = WEBRTC_STRING_EMPTY, b'\x00'
        elif isinstance(data, str):
            pp_id, user_data = WEBRTC_STRING, data.encode('utf8')
        elif data == b'':
            pp_id, user_data = WEBRTC_BINARY_EMPTY, b'\x00'
        else:
            pp_id, user_data = WEBRTC_BINARY, data

        self._data_channel_queue.append((channel, pp_id, user_data))
        await self._data_channel_flush()

    def __log_debug(self, msg, *args):
        logger.debug(self.role + ' ' + msg, *args)

    class State(enum.Enum):
        CLOSED = 1
        COOKIE_WAIT = 2
        COOKIE_ECHOED = 3
        ESTABLISHED = 4
        SHUTDOWN_PENDING = 5
        SHUTDOWN_SENT = 6
        SHUTDOWN_RECEIVED = 7
        SHUTDOWN_ACK_SENT = 8

import enum
import logging
import os
from struct import pack, unpack

import crcmod.predefined


crc32c = crcmod.predefined.mkPredefinedCrcFun('crc-32c')
logger = logging.getLogger('sctp')


STATE_COOKIE = 0x0007


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


def randl():
    return unpack('!L', os.urandom(4))[0]


def swapl(i):
    return unpack("<I", pack(">I", i))[0]


class Chunk:
    def __bytes__(self):
        body = self.body
        data = pack('!BBH', self.type, self.flags, len(body) + 4) + body
        data += b'\x00' * padl(len(body))
        return data


class ChunkType(enum.IntEnum):
    DATA = 0
    INIT = 1
    INIT_ACK = 2
    SACK = 3
    HEARTBEAT = 4
    HEARTBEAT_ACK = 5
    ABORT = 6
    SHUTDOWN = 7
    SHUTDOWN_ACK = 8
    ERROR = 9
    COOKIE_ECHO = 10
    COOKIE_ACK = 11
    SHUTDOWN_COMPLETE = 14


class AbortChunk(Chunk):
    type = ChunkType.ABORT

    def __init__(self, flags=0, body=None):
        self.flags = flags
        if body:
            self.params = decode_params(body)
        else:
            self.params = []

    @property
    def body(self):
        return encode_params(self.params)


class CookieAckChunk(Chunk):
    type = ChunkType.COOKIE_ACK

    def __init__(self, flags=0, body=None):
        self.flags = flags
        self.body = b''


class CookieEchoChunk(Chunk):
    type = ChunkType.COOKIE_ECHO

    def __init__(self, flags=0, body=b''):
        self.flags = flags
        self.body = body


class DataChunk(Chunk):
    type = ChunkType.DATA

    def __init__(self, flags=0, body=None):
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
        return pack('!LHHL', self.tsn, self.stream_id, self.stream_seq, self.protocol)


class InitChunk(Chunk):
    type = ChunkType.INIT

    def __init__(self, flags=0, body=None):
        self.flags = flags
        if body:
            (self.initiate_tag, self.advertise_rwnd, self.outbound_streams,
             self.inbound_streams, self.initial_tsn) = unpack('!LLHHL', body[0:16])
            self.params = decode_params(body[16:])
        else:
            self.initiate_tag = 0
            self.advertise_rwnd = 0
            self.outbound_streams = 0
            self.inbound_streams = 0
            self.initial_tsn = 0
            self.params = []

    @property
    def body(self):
        body = pack(
            '!LLHHL', self.initiate_tag, self.advertise_rwnd, self.outbound_streams,
            self.inbound_streams, self.initial_tsn)
        body += encode_params(self.params)
        return body


class InitAckChunk(InitChunk):
    type = ChunkType.INIT_ACK


class UnknownChunk(Chunk):
    def __init__(self, type, flags, body):
        self.type = type
        self.flags = flags
        self.body = body
        self.params = {}


class Packet:
    def __init__(self, source_port, destination_port, verification_tag):
        self.source_port = source_port
        self.destination_port = destination_port
        self.verification_tag = verification_tag
        self.chunks = []

    def __bytes__(self):
        checksum = 0
        data = pack(
            '!HHII',
            self.source_port,
            self.destination_port,
            self.verification_tag,
            checksum)
        for chunk in self.chunks:
            data += bytes(chunk)

        # calculate checksum
        checksum = swapl(crc32c(data))
        return data[0:8] + pack('!I', checksum) + data[12:]

    @classmethod
    def parse(cls, data):
        source_port, destination_port, verification_tag, checksum = unpack(
            '!HHII', data[0:12])

        # verify checksum
        check_data = data[0:8] + b'\x00\x00\x00\x00' + data[12:]
        if checksum != swapl(crc32c(check_data)):
            raise ValueError('Invalid checksum')

        packet = cls(
            source_port=source_port,
            destination_port=destination_port,
            verification_tag=verification_tag)

        pos = 12
        while pos <= len(data) - 4:
            chunk_type, chunk_flags, chunk_length = unpack('!BBH', data[pos:pos + 4])
            chunk_body = data[pos + 4:pos + chunk_length]
            if chunk_type == ChunkType.DATA:
                cls = DataChunk
            elif chunk_type == ChunkType.INIT:
                cls = InitChunk
            elif chunk_type == ChunkType.INIT_ACK:
                cls = InitAckChunk
            elif chunk_type == ChunkType.ABORT:
                cls = AbortChunk
            elif chunk_type == ChunkType.COOKIE_ECHO:
                cls = CookieEchoChunk
            elif chunk_type == ChunkType.COOKIE_ACK:
                cls = CookieAckChunk
            else:
                cls = None

            if cls:
                packet.chunks.append(cls(
                    flags=chunk_flags,
                    body=chunk_body))
            else:
                packet.chunks.append(UnknownChunk(
                    type=chunk_type,
                    flags=chunk_flags,
                    body=chunk_body))
            pos += chunk_length + padl(chunk_length)
        return packet


class Transport:
    def __init__(self, is_server, transport):
        self.is_server = is_server
        self.role = is_server and 'server' or 'client'
        self.local_initiate_tag = randl()
        self.local_tsn = randl()
        self.remote_initiate_tag = 0
        self.transport = transport

    async def receive_chunk(self, chunk):
        logger.info('%s < %s', self.role, chunk.__class__.__name__)
        if isinstance(chunk, InitChunk) and self.is_server:
            self.remote_initiate_tag = chunk.initiate_tag

            ack = InitAckChunk()
            ack.initiate_tag = self.local_initiate_tag
            ack.advertise_rwnd = 131072
            ack.outbound_streams = 256
            ack.inbound_streams = 2048
            ack.initial_tsn = self.local_tsn
            ack.params.append((STATE_COOKIE, b'12345678'))
            await self.send_chunk(ack)
        elif isinstance(chunk, InitAckChunk) and not self.is_server:
            echo = CookieEchoChunk()
            await self.send_chunk(echo)
        elif isinstance(chunk, CookieEchoChunk) and self.is_server:
            ack = CookieAckChunk()
            await self.send_chunk(ack)
        elif isinstance(chunk, DataChunk):
            print('tsn', chunk.tsn)
            print('proto', chunk.protocol)
            print('user_data', chunk.user_data)
            pass

    async def send_chunk(self, chunk):
        logger.info('%s > %s', self.role, chunk.__class__.__name__)
        packet = Packet(
            source_port=5000,
            destination_port=5000,
            verification_tag=self.remote_initiate_tag)
        packet.chunks.append(chunk)
        await self.transport.send(bytes(packet))

    async def run(self):
        if not self.is_server:
            chunk = InitChunk()
            chunk.initiate_tag = self.local_initiate_tag
            chunk.advertise_rwnd = 131072
            chunk.outbound_streams = 256
            chunk.inbound_streams = 2048
            chunk.initial_tsn = self.local_tsn
            await self.send_chunk(chunk)

        while True:
            data = await self.transport.recv()
            try:
                packet = Packet.parse(data)
            except ValueError:
                continue

            for chunk in packet.chunks:
                await self.receive_chunk(chunk)

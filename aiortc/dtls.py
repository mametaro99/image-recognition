import asyncio
import base64
import enum
import logging
import os
import sys

from cryptography.hazmat.bindings.openssl.binding import Binding
from pylibsrtp import Policy, Session

from .rtp import is_rtcp
from .utils import first_completed

binding = Binding()
binding.init_static_locks()
ffi = binding.ffi
lib = binding.lib

SRTP_KEY_LEN = 16
SRTP_SALT_LEN = 14

CERT_PATH = os.path.join(os.path.dirname(__file__), 'dtls.crt')
KEY_PATH = os.path.join(os.path.dirname(__file__), 'dtls.key')


logger = logging.getLogger('dtls')


def _openssl_assert(ok):
    if not ok:
        raise Exception('OpenSSL call failed')


def certificate_digest(x509):
    digest = lib.EVP_get_digestbyname(b'SHA256')
    if digest == ffi.NULL:
        raise ValueError("No such digest method")

    result_buffer = ffi.new('unsigned char[]', lib.EVP_MAX_MD_SIZE)
    result_length = ffi.new('unsigned int[]', 1)
    result_length[0] = len(result_buffer)

    digest_result = lib.X509_digest(x509, digest, result_buffer, result_length)
    assert digest_result == 1

    return b":".join([
        base64.b16encode(ch).upper() for ch
        in ffi.buffer(result_buffer, result_length[0])]).decode('ascii')


def get_srtp_key_salt(src, idx):
    key_start = idx * SRTP_KEY_LEN
    salt_start = 2 * SRTP_KEY_LEN + idx * SRTP_SALT_LEN
    return (
        src[key_start:key_start + SRTP_KEY_LEN] +
        src[salt_start:salt_start + SRTP_SALT_LEN]
    )


@ffi.callback('int(int, X509_STORE_CTX *)')
def verify_callback(x, y):
    return 1


class DtlsSrtpContext:
    def __init__(self):
        ctx = lib.SSL_CTX_new(lib.DTLSv1_method())
        self.ctx = ffi.gc(ctx, lib.SSL_CTX_free)

        lib.SSL_CTX_set_verify(self.ctx, lib.SSL_VERIFY_PEER | lib.SSL_VERIFY_FAIL_IF_NO_PEER_CERT,
                               verify_callback)

        _openssl_assert(lib.SSL_CTX_use_certificate_file(
            self.ctx,
            CERT_PATH.encode(sys.getfilesystemencoding()),
            lib.SSL_FILETYPE_PEM) == 1)
        _openssl_assert(lib.SSL_CTX_use_PrivateKey_file(
            self.ctx,
            KEY_PATH.encode(sys.getfilesystemencoding()),
            lib.SSL_FILETYPE_PEM) == 1)
        _openssl_assert(lib.SSL_CTX_set_cipher_list(self.ctx, b'HIGH:!CAMELLIA:!aNULL') == 1)
        _openssl_assert(lib.SSL_CTX_set_tlsext_use_srtp(self.ctx, b'SRTP_AES128_CM_SHA1_80') == 0)
        _openssl_assert(lib.SSL_CTX_set_read_ahead(self.ctx, 1) == 0)


class Channel:
    def __init__(self, closed, queue, send):
        self.closed = closed
        self.queue = queue
        self.send = send

    async def recv(self):
        data = await first_completed(self.queue.get(), self.closed.wait())
        if data is True:
            raise ConnectionError
        return data


class DtlsSrtpSession:
    def __init__(self, context, is_server, transport):
        self.closed = asyncio.Event()
        self.encrypted = False
        self.is_server = is_server
        self.remote_fingerprint = None
        self.role = self.is_server and 'server' or 'client'
        self.state = self.State.CLOSED
        self.transport = transport

        self.data_queue = asyncio.Queue()
        self.data = Channel(
            closed=self.closed,
            queue=self.data_queue,
            send=self._send_data)

        self.rtp_queue = asyncio.Queue()
        self.rtp = Channel(
            closed=self.closed,
            queue=self.rtp_queue,
            send=self._send_rtp)

        ssl = lib.SSL_new(context.ctx)
        self.ssl = ffi.gc(ssl, lib.SSL_free)

        self.read_bio = lib.BIO_new(lib.BIO_s_mem())
        self.read_cdata = ffi.new('char[]', 1500)
        self.write_bio = lib.BIO_new(lib.BIO_s_mem())
        self.write_cdata = ffi.new('char[]', 1500)
        lib.SSL_set_bio(self.ssl, self.read_bio, self.write_bio)

        if self.is_server:
            lib.SSL_set_accept_state(self.ssl)
        else:
            lib.SSL_set_connect_state(self.ssl)

        # local fingerprint
        x509 = lib.SSL_get_certificate(self.ssl)
        self.local_fingerprint = certificate_digest(x509)

    async def close(self):
        if self.state != self.State.CLOSED:
            lib.SSL_shutdown(self.ssl)
            await self._write_ssl()
            logger.debug('%s - DTLS shutdown complete', self.role)
            self.closed.set()

    async def connect(self):
        assert self.state == self.State.CLOSED

        self._set_state(self.State.CONNECTING)
        while not self.encrypted:
            result = lib.SSL_do_handshake(self.ssl)
            await self._write_ssl()

            if result > 0:
                self.encrypted = True
                break

            error = lib.SSL_get_error(self.ssl, result)
            if error == lib.SSL_ERROR_WANT_READ:
                await self._recv_next()
            else:
                raise Exception('DTLS handshake failed (error %d)' % error)

        # check remote fingerprint
        x509 = lib.SSL_get_peer_certificate(self.ssl)
        remote_fingerprint = certificate_digest(x509)
        if remote_fingerprint != self.remote_fingerprint.upper():
            raise Exception('DTLS fingerprint does not match')

        # generate keying material
        buf = ffi.new('unsigned char[]', 2 * (SRTP_KEY_LEN + SRTP_SALT_LEN))
        extractor = b'EXTRACTOR-dtls_srtp'
        if not lib.SSL_export_keying_material(self.ssl, buf, len(buf),
                                              extractor, len(extractor),
                                              ffi.NULL, 0, 0):
            raise Exception('DTLS could not extract SRTP keying material')

        view = ffi.buffer(buf)
        if self.is_server:
            srtp_tx_key = get_srtp_key_salt(view, 1)
            srtp_rx_key = get_srtp_key_salt(view, 0)
        else:
            srtp_tx_key = get_srtp_key_salt(view, 0)
            srtp_rx_key = get_srtp_key_salt(view, 1)

        rx_policy = Policy(key=srtp_rx_key, ssrc_type=Policy.SSRC_ANY_INBOUND)
        self._rx_srtp = Session(rx_policy)
        tx_policy = Policy(key=srtp_tx_key, ssrc_type=Policy.SSRC_ANY_OUTBOUND)
        self._tx_srtp = Session(tx_policy)

        # start data pump
        logger.debug('%s - DTLS handshake complete', self.role)
        self._set_state(self.State.CONNECTED)
        asyncio.ensure_future(self.__run())

    async def __run(self):
        try:
            while True:
                await self._recv_next()
        except ConnectionError:
            pass
        finally:
            self._set_state(self.State.CLOSED)
            self.closed.set()

    async def _recv_next(self):
        data = await first_completed(self.transport.recv(), self.closed.wait())
        if data is True:
            # session was closed
            raise ConnectionError

        first_byte = data[0]
        if first_byte > 19 and first_byte < 64:
            # DTLS
            lib.BIO_write(self.read_bio, data, len(data))
            result = lib.SSL_read(self.ssl, self.read_cdata, len(self.read_cdata))
            if result == 0:
                logger.debug('%s - DTLS shutdown by remote party' % self.role)
                raise ConnectionError
            elif result > 0:
                await self.data_queue.put(ffi.buffer(self.read_cdata)[0:result])
        elif first_byte > 127 and first_byte < 192:
            # SRTP / SRTCP
            if is_rtcp(data):
                data = self._rx_srtp.unprotect_rtcp(data)
            else:
                data = self._rx_srtp.unprotect(data)
            await self.rtp_queue.put(data)

    async def _send_data(self, data):
        lib.SSL_write(self.ssl, data, len(data))
        await self._write_ssl()

    async def _send_rtp(self, data):
        if is_rtcp(data):
            data = self._tx_srtp.protect_rtcp(data)
        else:
            data = self._tx_srtp.protect(data)
        await self.transport.send(data)

    def _set_state(self, state):
        if state != self.state:
            logger.debug('%s - %s -> %s', self.role, self.state, state)
            self.state = state

    async def _write_ssl(self):
        """
        Flush outgoing data which OpenSSL put in our BIO to the transport.
        """
        pending = lib.BIO_ctrl_pending(self.write_bio)
        if pending > 0:
            result = lib.BIO_read(self.write_bio, self.write_cdata, len(self.write_cdata))
            await self.transport.send(ffi.buffer(self.write_cdata)[0:result])

    class State(enum.Enum):
        CLOSED = 0
        CONNECTING = 1
        CONNECTED = 2

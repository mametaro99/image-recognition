import ipaddress
import re

from . import rtp
from .rtcdtlstransport import RTCDtlsFingerprint, RTCDtlsParameters
from .rtcicetransport import RTCIceCandidate, RTCIceParameters
from .rtcrtpparameters import (RTCRtpCodecParameters,
                               RTCRtpHeaderExtensionParameters,
                               RTCRtpParameters)
from .rtcsctptransport import RTCSctpCapabilities

DIRECTIONS = [
    'sendrecv',
    'sendonly',
    'recvonly',
    'inactive'
]

DTLS_ROLE_SETUP = {
    'auto': 'actpass',
    'client': 'active',
    'server': 'passive'
}
DTLS_SETUP_ROLE = dict([(v, k) for (k, v) in DTLS_ROLE_SETUP.items()])


def candidate_from_sdp(sdp):
    bits = sdp.split()
    assert len(bits) >= 8

    candidate = RTCIceCandidate(
        component=int(bits[1]),
        foundation=bits[0],
        ip=bits[4],
        port=int(bits[5]),
        priority=int(bits[3]),
        protocol=bits[2],
        type=bits[7])

    for i in range(8, len(bits) - 1, 2):
        if bits[i] == 'tcptype':
            candidate.tcpType = bits[i + 1]

    return candidate


def candidate_to_sdp(candidate):
    sdp = '%s %d %s %d %s %d typ %s' % (
        candidate.foundation,
        candidate.component,
        candidate.protocol,
        candidate.priority,
        candidate.ip,
        candidate.port,
        candidate.type)

    if candidate.tcpType:
        sdp += ' tcptype %s' % candidate.tcpType
    return sdp


def ipaddress_from_sdp(sdp):
    m = re.match('^IN (IP4|IP6) ([^ ]+)$', sdp)
    assert m
    return m.group(2)


def ipaddress_to_sdp(addr):
    version = ipaddress.ip_address(addr).version
    return 'IN IP%d %s' % (version, addr)


class MediaDescription:
    def __init__(self, kind, port, profile, fmt):
        # rtp
        self.kind = kind
        self.port = port
        self.host = None
        self.profile = profile
        self.direction = None

        # rtcp
        self.rtcp_port = None
        self.rtcp_host = None

        # formats
        self.fmt = fmt
        self.rtp = RTCRtpParameters()
        self.sctpmap = {}

        # SCTP
        self.sctpCapabilities = None

        # DTLS
        self.dtls = RTCDtlsParameters()

        # ICE
        self.ice = RTCIceParameters()
        self.ice_candidates = []
        self.ice_candidates_complete = False

    def __str__(self):
        lines = []
        lines.append('m=%s %d %s %s' % (
            self.kind,
            self.port,
            self.profile,
            ' '.join(map(str, self.fmt))
        ))
        lines.append('c=%s' % ipaddress_to_sdp(self.host))
        if self.direction is not None:
            lines.append('a=' + self.direction)

        for header in self.rtp.headerExtensions:
            lines.append('a=extmap:%d %s' % (header.id, header.uri))

        if self.rtp.muxId:
            lines.append('a=mid:' + self.rtp.muxId)

        if self.rtcp_port is not None and self.rtcp_host is not None:
            lines.append('a=rtcp:%d %s' % (self.rtcp_port, ipaddress_to_sdp(self.rtcp_host)))
            if self.rtp.rtcp.mux:
                lines.append('a=rtcp-mux')
            if self.rtp.rtcp.ssrc and self.rtp.rtcp.cname:
                lines.append('a=ssrc:%d cname:%s' % (self.rtp.rtcp.ssrc, self.rtp.rtcp.cname))

        for codec in self.rtp.codecs:
            lines.append('a=rtpmap:%d %s' % (codec.payloadType, codec))

        for k, v in self.sctpmap.items():
            lines.append('a=sctpmap:%d %s' % (k, v))
        if self.sctpCapabilities is not None:
            lines.append('a=max-message-size:%d' % self.sctpCapabilities.maxMessageSize)

        # ice
        for candidate in self.ice_candidates:
            lines.append('a=candidate:' + candidate_to_sdp(candidate))
        if self.ice_candidates_complete:
            lines.append('a=end-of-candidates')
        if self.ice.usernameFragment is not None:
            lines.append('a=ice-ufrag:' + self.ice.usernameFragment)
        if self.ice.password is not None:
            lines.append('a=ice-pwd:' + self.ice.password)

        # dtls
        for fingerprint in self.dtls.fingerprints:
            lines.append('a=fingerprint:%s %s' % (fingerprint.algorithm, fingerprint.value))
        lines.append('a=setup:' + DTLS_ROLE_SETUP[self.dtls.role])

        return '\r\n'.join(lines) + '\r\n'


class SessionDescription:
    def __init__(self):
        self.version = 0
        self.origin = None
        self.name = '-'
        self.time = '0 0'
        self.bundle = []
        self.media = []

    @classmethod
    def parse(cls, sdp):
        current_media = None
        dtls_fingerprints = []
        session = cls()

        for line in sdp.splitlines():
            if line.startswith('o='):
                session.origin = line.strip()[2:]
            if line.startswith('m='):
                m = re.match('^m=([^ ]+) ([0-9]+) ([A-Z/]+) (.+)$', line)
                assert m

                # check payload types are valid
                kind = m.group(1)
                fmt = [int(x) for x in m.group(4).split()]
                if kind in ['audio', 'video']:
                    for pt in fmt:
                        assert pt >= 0 and pt < 256
                        assert pt not in rtp.FORBIDDEN_PAYLOAD_TYPES

                current_media = MediaDescription(
                    kind=kind,
                    port=int(m.group(2)),
                    profile=m.group(3),
                    fmt=fmt)
                current_media.dtls.fingerprints = dtls_fingerprints
                session.media.append(current_media)
            elif line.startswith('c=') and current_media:
                current_media.host = ipaddress_from_sdp(line[2:])
            elif line.startswith('a='):
                if ':' in line:
                    attr, value = line[2:].split(':', 1)
                else:
                    attr = line[2:]
                if current_media:
                    if attr == 'candidate':
                        current_media.ice_candidates.append(candidate_from_sdp(value))
                    elif attr == 'end-of-candidates':
                        current_media.ice_candidates_complete = True
                    elif attr == 'extmap':
                        ext_id, ext_uri = value.split()
                        if '/' in ext_id:
                            ext_id, ext_direction = ext_id.split('/')
                        extension = RTCRtpHeaderExtensionParameters(id=int(ext_id), uri=ext_uri)
                        current_media.rtp.headerExtensions.append(extension)
                    elif attr == 'fingerprint':
                        algorithm, fingerprint = value.split()
                        current_media.dtls.fingerprints.append(RTCDtlsFingerprint(
                            algorithm=algorithm,
                            value=fingerprint))
                    elif attr == 'ice-ufrag':
                        current_media.ice.usernameFragment = value
                    elif attr == 'ice-pwd':
                        current_media.ice.password = value
                    elif attr == 'max-message-size':
                        current_media.sctpCapabilities = RTCSctpCapabilities(
                            maxMessageSize=int(value))
                    elif attr == 'mid':
                        current_media.rtp.muxId = value
                    elif attr == 'rtcp':
                        port, rest = value.split(' ', 1)
                        current_media.rtcp_port = int(port)
                        current_media.rtcp_host = ipaddress_from_sdp(rest)
                    elif attr == 'rtcp-mux':
                        current_media.rtp.rtcp.mux = True
                    elif attr == 'setup':
                        current_media.dtls.role = DTLS_SETUP_ROLE[value]
                    elif attr in DIRECTIONS:
                        current_media.direction = attr
                    elif attr == 'rtpmap':
                        format_id, format_desc = value.split(' ', 1)
                        format_id = int(format_id)
                        bits = format_desc.split('/')
                        codec = RTCRtpCodecParameters(
                            name=bits[0],
                            channels=int(bits[2]) if len(bits) > 2 else None,
                            clockRate=int(bits[1]),
                            payloadType=int(format_id))
                        current_media.rtp.codecs.append(codec)
                    elif attr == 'sctpmap':
                        format_id, format_desc = value.split(' ', 1)
                        getattr(current_media, attr)[int(format_id)] = format_desc
                    elif attr == 'ssrc':
                        ssrc, ssrc_desc = value.split(' ', 1)
                        ssrc_attr, ssrc_value = ssrc_desc.split(':')
                        if ssrc_attr == 'cname':
                            current_media.rtp.rtcp.cname = ssrc_value
                            current_media.rtp.rtcp.ssrc = int(ssrc)
                else:
                    # session-level attributes
                    if attr == 'fingerprint':
                        algorithm, fingerprint = value.split()
                        dtls_fingerprints.append(RTCDtlsFingerprint(
                            algorithm=algorithm,
                            value=fingerprint))
                    elif attr == 'group':
                        bits = value.split()
                        if bits and bits[0] == 'BUNDLE':
                            session.bundle = bits[1:]

        return session

    def __str__(self):
        lines = [
            'v=%d' % self.version,
            'o=%s' % self.origin,
            's=%s' % self.name,
            't=%s' % self.time,
        ]
        if self.bundle:
            lines += ['a=group:BUNDLE ' + (' '.join(self.bundle))]
        return '\r\n'.join(lines) + '\r\n' + ''.join([str(m) for m in self.media])

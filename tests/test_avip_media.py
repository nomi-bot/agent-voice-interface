import json
from pathlib import Path
import struct
import unittest

from avi.protocols import avip, media


FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "avip-v1-media.json").read_text()
)
_DISTINCTIVE = b"SECRET!"


def _frame(direction=media.Direction.AUDIO_UPLINK, sequence=0, timestamp_us=0,
           payload=b"\x00\x00", codec=media.Codec.PCM_S16LE):
    return media.MediaFrame(direction, sequence, timestamp_us, payload, codec)


class MediaProfileTests(unittest.TestCase):
    def test_shared_fixture_matches_the_m0_profile(self):
        self.assertEqual(FIXTURES["endianness"], media.ENDIANNESS)
        self.assertEqual(FIXTURES["header_bytes"], media.HEADER_SIZE)
        self.assertEqual(bytes.fromhex(FIXTURES["magic_hex"]), media.MAGIC)
        self.assertEqual(FIXTURES["version"], media.VERSION)
        self.assertEqual(FIXTURES["protocol"], avip.PROTOCOL)
        self.assertEqual(FIXTURES["codec"]["id"], media.Codec.PCM_S16LE)
        self.assertEqual(FIXTURES["sample_rate_hz"], media.SAMPLE_RATE_HZ)
        self.assertEqual(FIXTURES["channels"], media.CHANNELS)
        self.assertEqual(FIXTURES["sample_width_bytes"], media.SAMPLE_WIDTH_BYTES)
        self.assertEqual(FIXTURES["nominal_chunk_us"], media.NOMINAL_CHUNK_US)
        self.assertEqual(FIXTURES["nominal_payload_bytes"], media.NOMINAL_PAYLOAD_BYTES)
        self.assertEqual(FIXTURES["nominal_payload_bytes"], 640)
        self.assertEqual(FIXTURES["max_payload_bytes"], media.MAX_PAYLOAD_BYTES)
        self.assertEqual(FIXTURES["max_queue_depth"], media.MAX_QUEUE_DEPTH)
        self.assertEqual(FIXTURES["queue_policy"], "reject_newest")
        self.assertIs(media.ProtocolError, avip.ProtocolError)

    def test_golden_vectors_are_little_endian_and_round_trip(self):
        for vector in FIXTURES["vectors"]:
            with self.subTest(vector=vector["name"]):
                raw = bytes.fromhex(vector["frame_hex"])
                payload = bytes.fromhex(vector["payload_hex"])
                direction = media.Direction(FIXTURES["directions"][vector["direction"]])
                self.assertEqual(raw[:4], media.MAGIC)
                self.assertEqual(raw[media.VERSION_OFFSET], media.VERSION)
                self.assertEqual(raw[media.DIRECTION_OFFSET], direction)
                self.assertEqual(raw[media.CODEC_OFFSET], media.Codec.PCM_S16LE)
                self.assertEqual(raw[media.RESERVED_OFFSET], 0)
                self.assertEqual(
                    raw[media.SEQUENCE_OFFSET:media.TIMESTAMP_OFFSET],
                    vector["sequence"].to_bytes(4, "little"),
                )
                self.assertEqual(
                    raw[media.TIMESTAMP_OFFSET:media.PAYLOAD_LENGTH_OFFSET],
                    vector["timestamp_us"].to_bytes(8, "little"),
                )
                self.assertEqual(
                    raw[media.PAYLOAD_LENGTH_OFFSET:media.HEADER_SIZE],
                    len(payload).to_bytes(4, "little"),
                )
                self.assertEqual(raw[media.HEADER_SIZE:], payload)
                frame = media.unpack_frame(raw)
                self.assertEqual(frame.direction, direction)
                self.assertEqual(frame.sequence, vector["sequence"])
                self.assertEqual(frame.timestamp_us, vector["timestamp_us"])
                self.assertEqual(frame.payload, payload)
                self.assertEqual(frame.codec, media.Codec.PCM_S16LE)
                self.assertEqual(media.pack_frame(frame), raw)

    def test_nominal_chunk_and_header_size_are_stable(self):
        frame = _frame(payload=bytes(media.NOMINAL_PAYLOAD_BYTES))
        packed = media.pack_frame(frame)
        self.assertEqual(len(packed), media.HEADER_SIZE + 640)
        self.assertEqual(media.unpack_frame(packed), frame)

    def test_memoryviews_round_trip_without_sharing_storage(self):
        source = bytearray(bytes.fromhex(FIXTURES["vectors"][0]["frame_hex"]))
        frame = media.unpack_frame(memoryview(source))
        source[:] = b"\xff" * len(source)
        self.assertEqual(frame.payload, bytes.fromhex("00000000"))
        payload = bytearray(b"\x01\x02\x03\x04")
        built = _frame(payload=memoryview(payload))
        payload[0] = 0xFF
        self.assertEqual(built.payload, b"\x01\x02\x03\x04")
        self.assertEqual(media.unpack_frame(memoryview(media.pack_frame(built))), built)


class FrameValidationTests(unittest.TestCase):
    def setUp(self):
        self.packed = media.pack_frame(_frame(payload=_DISTINCTIVE + b"\x00"))

    def _replace(self, offset, value):
        raw = bytearray(self.packed)
        raw[offset] = value
        return bytes(raw)

    def test_non_byte_buffers_are_rejected_without_their_contents(self):
        for invalid in (None, "AVIP", 24, [], {}, 1.5, True):
            with self.subTest(value=type(invalid).__name__):
                with self.assertRaises(media.ProtocolError) as caught:
                    media.unpack_frame(invalid)
                self.assertEqual(caught.exception.code, "invalid_frame")
                self.assertIsNone(caught.exception.field)
                self.assertNotIn("AVIP", str(caught.exception))

    def test_short_frames_fail_before_magic_is_trusted(self):
        with self.assertRaises(media.ProtocolError) as caught:
            media.unpack_frame(b"NOPE")
        self.assertEqual((caught.exception.code, caught.exception.field),
                         ("invalid_frame", "header"))
        self.assertNotIn("NOPE", str(caught.exception))

    def test_bad_magic_version_reserved_direction_and_codec_are_stable(self):
        cases = (
            (self.packed[:0] + b"NOPE" + self.packed[4:], "unsupported_protocol", "magic", "NOPE"),
            (self._replace(media.VERSION_OFFSET, 2), "unsupported_protocol", "version", "2"),
            (self._replace(media.RESERVED_OFFSET, 1), "invalid_frame", "reserved", None),
            (self._replace(media.DIRECTION_OFFSET, 0), "invalid_frame", "direction", None),
            (self._replace(media.DIRECTION_OFFSET, 9), "invalid_frame", "direction", "9"),
            (self._replace(media.CODEC_OFFSET, 2), "unsupported_codec", "codec", "2"),
            (self._replace(media.CODEC_OFFSET, 255), "unsupported_codec", "codec", "255"),
        )
        for buffer, code, field, leaked in cases:
            with self.subTest(code=code, field=field):
                with self.assertRaises(media.ProtocolError) as caught:
                    media.unpack_frame(buffer)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.field, field)
                if leaked is not None:
                    self.assertNotIn(leaked, str(caught.exception))
                    self.assertNotIn(leaked, repr(caught.exception))

    def test_version_is_reported_before_an_unknown_codec(self):
        raw = bytearray(self.packed)
        raw[media.VERSION_OFFSET] = 2
        raw[media.CODEC_OFFSET] = 9
        with self.assertRaises(media.ProtocolError) as caught:
            media.unpack_frame(bytes(raw))
        self.assertEqual(caught.exception.field, "version")

    def test_oversized_frames_do_not_echo_the_declared_length(self):
        huge = b"\x00" * (media.MAX_FRAME_BYTES + 1)
        with self.assertRaises(media.ProtocolError) as caught:
            media.unpack_frame(huge)
        self.assertEqual((caught.exception.code, caught.exception.field),
                         ("frame_too_large", "payload"))
        declared = struct.pack("<4sBBBBIQI", media.MAGIC, 1, 1, 1, 0, 0, 0, 0xFFFFFFFF)
        with self.assertRaises(media.ProtocolError) as caught:
            media.unpack_frame(declared)
        self.assertEqual(caught.exception.code, "frame_too_large")
        self.assertNotIn("4294967295", str(caught.exception))
        self.assertNotIn("4294967295", repr(caught.exception))
        crafted = struct.pack("<4sBBBBIQI", b"NOPE", 9, 9, 9, 9, 0, 0, 9000) + b"\x00\x00"
        oversized = crafted + bytes(media.MAX_PAYLOAD_BYTES)
        with self.assertRaises(media.ProtocolError) as caught:
            media.unpack_frame(oversized)
        self.assertEqual(caught.exception.code, "frame_too_large")
        self.assertNotIn("NOPE", str(caught.exception))

    def test_payload_bounds_and_pcm_alignment_are_enforced(self):
        header = struct.pack("<4sBBBBIQI", media.MAGIC, 1, 1, 1, 0, 0, 0, 4)
        for buffer in (header + b"\x00\x00", header[:-4] + struct.pack("<I", 0),
                       header[:-4] + struct.pack("<I", 3) + b"\x00\x00\x00"):
            with self.subTest(length=len(buffer)):
                with self.assertRaises(media.ProtocolError) as caught:
                    media.unpack_frame(buffer)
                self.assertEqual((caught.exception.code, caught.exception.field),
                                 ("invalid_frame", "payload"))
        largest = _frame(payload=bytes(media.MAX_PAYLOAD_BYTES))
        self.assertEqual(len(media.pack_frame(largest)), media.MAX_FRAME_BYTES)
        with self.assertRaises(media.ProtocolError) as caught:
            _frame(payload=bytes(media.MAX_PAYLOAD_BYTES + 2))
        self.assertEqual(caught.exception.code, "frame_too_large")

    def test_big_endian_header_integers_are_not_accepted_as_the_same_frame(self):
        payload = b"\x00\x00\xff\x7f"
        foreign = struct.pack(">4sBBBBIQI", media.MAGIC, 1, 1, 1, 0, 1, 20000, len(payload))
        foreign += payload
        with self.assertRaises(media.ProtocolError):
            media.unpack_frame(foreign)

    def test_constructor_rejects_illegal_field_values_without_echoing_them(self):
        cases = (
            {"direction": 0},
            {"direction": True},
            {"sequence": -1},
            {"sequence": 2**32},
            {"sequence": True},
            {"timestamp_us": -1},
            {"timestamp_us": 2**64},
            {"payload": b"\x00"},
            {"payload": "00"},
            {"codec": 2},
            {"codec": "pcm_s16le"},
        )
        for update in cases:
            with self.subTest(update=tuple(update)):
                with self.assertRaises(media.ProtocolError) as caught:
                    _frame(**update)
                rendered = f"{caught.exception} {caught.exception!r}"
                self.assertNotIn(str(next(iter(update.values()))), rendered)

    def test_non_frames_cannot_be_packed(self):
        with self.assertRaises(media.ProtocolError) as caught:
            media.pack_frame(b"AVIP")
        self.assertEqual(caught.exception.code, "invalid_frame")
        self.assertNotIn("AVIP", str(caught.exception))

    def test_integer_direction_and_codec_are_canonicalized(self):
        frame = media.MediaFrame(1, 0, 0, b"\x00\x00", 1)
        self.assertIs(frame.direction, media.Direction.AUDIO_UPLINK)
        self.assertIs(frame.codec, media.Codec.PCM_S16LE)

    def test_wide_memoryviews_are_rejected(self):
        view = memoryview(bytearray(b"\x00\x00")).cast("h")
        with self.assertRaises(media.ProtocolError) as caught:
            _frame(payload=view)
        self.assertEqual(caught.exception.field, "payload")


class StreamContinuityTests(unittest.TestCase):
    def test_each_direction_starts_at_zero_and_increments_independently(self):
        stream = media.MediaStream()
        uplink = _frame(sequence=0, timestamp_us=0)
        late = _frame(sequence=1, timestamp_us=20_000)
        with self.assertRaises(media.ProtocolError) as caught:
            stream.accept(late)
        self.assertEqual((caught.exception.code, caught.exception.field),
                         ("sequence_anomaly", "sequence"))
        stream.accept(uplink)
        stream.accept(late)
        downlink = _frame(media.Direction.AUDIO_DOWNLINK, 0, 5_000)
        stream.accept(downlink)
        with self.assertRaises(media.ProtocolError) as caught:
            stream.accept(late)
        self.assertEqual(caught.exception.code, "sequence_anomaly")
        stream.accept(_frame(sequence=2, timestamp_us=20_000))

    def test_duplicate_gap_and_rewind_do_not_advance_state(self):
        stream = media.MediaStream()
        stream.accept(_frame(sequence=0, timestamp_us=10))
        for sequence in (0, 2, 0xFFFFFFFF):
            with self.subTest(sequence=sequence):
                with self.assertRaises(media.ProtocolError) as caught:
                    stream.accept(_frame(sequence=sequence, timestamp_us=10))
                self.assertEqual(caught.exception.code, "sequence_anomaly")
                self.assertNotIn(str(sequence), str(caught.exception))
        stream.accept(_frame(sequence=1, timestamp_us=10))

    def test_timestamps_may_repeat_but_must_not_move_backward(self):
        stream = media.MediaStream()
        stream.accept(_frame(timestamp_us=50))
        stream.accept(_frame(sequence=1, timestamp_us=50))
        with self.assertRaises(media.ProtocolError) as caught:
            stream.accept(_frame(sequence=2, timestamp_us=49))
        self.assertEqual((caught.exception.code, caught.exception.field),
                         ("timestamp_regression", "timestamp_us"))
        self.assertNotIn("49", str(caught.exception))
        stream.accept(_frame(sequence=2, timestamp_us=51))

    def test_sequence_wraps_with_modulo_arithmetic(self):
        self.assertEqual(media.next_sequence(0xFFFFFFFF), 0)
        self.assertEqual(media.next_sequence(0), 1)
        with self.assertRaises(media.ProtocolError):
            media.next_sequence(True)
        with self.assertRaises(media.ProtocolError):
            media.next_sequence(-1)
        stream = media.MediaStream()
        # Reach the uint32 boundary without accepting 2**32 frames.
        stream._expected[media.Direction.AUDIO_UPLINK] = 0xFFFFFFFF
        stream.accept(_frame(sequence=0xFFFFFFFF, timestamp_us=10))
        self.assertEqual(stream._expected[media.Direction.AUDIO_UPLINK], 0)
        stream.accept(_frame(sequence=0, timestamp_us=10))
        with self.assertRaises(media.ProtocolError) as caught:
            stream.accept(_frame(sequence=0xFFFFFFFF, timestamp_us=10))
        self.assertEqual(caught.exception.code, "sequence_anomaly")
        with self.assertRaises(media.ProtocolError) as caught:
            stream.accept(_frame(sequence=1, timestamp_us=9))
        self.assertEqual(caught.exception.code, "timestamp_regression")
        stream.accept(_frame(sequence=1, timestamp_us=10))


class BoundedDeliveryTests(unittest.TestCase):
    def test_full_queue_rejects_the_newest_frame_and_keeps_the_oldest(self):
        queue = media.MediaQueue(max_depth=1)
        first = _frame(payload=b"\x00\x01")
        rejected = _frame(sequence=1, payload=_DISTINCTIVE + b"\x00")
        queue.offer(first)
        with self.assertRaises(media.ProtocolError) as caught:
            queue.offer(rejected)
        self.assertEqual((caught.exception.code, caught.exception.field), ("queue_full", None))
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("SECRET", repr(caught.exception))
        self.assertIs(queue.take(), first)
        self.assertIsNone(queue.take())
        self.assertEqual(len(queue), 0)

    def test_depth_is_capped_and_flush_is_explicit(self):
        self.assertEqual(media.MediaQueue().max_depth, media.MAX_QUEUE_DEPTH)
        queue = media.MediaQueue(media.MAX_QUEUE_DEPTH)
        for sequence in range(media.MAX_QUEUE_DEPTH):
            queue.offer(_frame(sequence=sequence))
        with self.assertRaises(media.ProtocolError) as caught:
            queue.offer(_frame(sequence=media.MAX_QUEUE_DEPTH))
        self.assertEqual(caught.exception.code, "queue_full")
        self.assertEqual(queue.clear(), media.MAX_QUEUE_DEPTH)
        self.assertIsNone(queue.take())
        for invalid in (0, -1, media.MAX_QUEUE_DEPTH + 1, True, None, 1.0):
            with self.subTest(value=invalid):
                with self.assertRaises(ValueError) as caught:
                    media.MediaQueue(invalid)
                self.assertEqual(str(caught.exception),
                                 f"max_depth must be an integer from 1 to {media.MAX_QUEUE_DEPTH}")
                self.assertNotIn(repr(invalid), str(caught.exception))

    def test_queue_rejects_objects_that_are_not_frames(self):
        queue = media.MediaQueue(1)
        with self.assertRaises(media.ProtocolError) as caught:
            queue.offer(_DISTINCTIVE)
        self.assertEqual(caught.exception.code, "invalid_frame")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_delivery_does_not_consume_sequence_when_the_queue_is_full(self):
        delivery = media.MediaDelivery(max_depth=1)
        first = _frame(sequence=0, timestamp_us=0, payload=b"\x10\x00")
        second = _frame(sequence=1, timestamp_us=20_000, payload=b"\x20\x00")
        delivery.submit(first)
        with self.assertRaises(media.ProtocolError) as caught:
            delivery.submit(second)
        self.assertEqual(caught.exception.code, "queue_full")
        with self.assertRaises(media.ProtocolError) as caught:
            delivery.submit(second)
        self.assertEqual(caught.exception.code, "queue_full")
        self.assertIs(delivery.take(media.Direction.AUDIO_UPLINK), first)
        delivery.submit(second)
        self.assertEqual(delivery.depth(1), 1)
        with self.assertRaises(media.ProtocolError) as caught:
            delivery.submit(_frame(sequence=3, timestamp_us=40_000))
        self.assertEqual(caught.exception.code, "sequence_anomaly")
        self.assertEqual(delivery.depth(media.Direction.AUDIO_UPLINK), 1)

    def test_directions_have_independent_bounds_and_flush_keeps_sequence(self):
        delivery = media.MediaDelivery(max_depth=1)
        delivery.submit(_frame())
        delivery.submit(_frame(media.Direction.AUDIO_DOWNLINK, payload=b"\x02\x00"))
        with self.assertRaises(media.ProtocolError):
            delivery.submit(_frame(sequence=1, timestamp_us=1))
        self.assertEqual(delivery.depth(media.Direction.AUDIO_DOWNLINK), 1)
        self.assertEqual(delivery.clear(media.Direction.AUDIO_UPLINK), 1)
        self.assertEqual(delivery.depth(media.Direction.AUDIO_DOWNLINK), 1)
        with self.assertRaises(media.ProtocolError) as caught:
            delivery.submit(_frame(sequence=0, timestamp_us=1))
        self.assertEqual(caught.exception.code, "sequence_anomaly")
        delivery.submit(_frame(sequence=1, timestamp_us=1))
        self.assertEqual(delivery.clear(), 2)
        self.assertIsNone(delivery.take(media.Direction.AUDIO_UPLINK))
        self.assertIsNone(delivery.take(media.Direction.AUDIO_DOWNLINK))

    def test_ingest_decodes_then_enforces_order(self):
        delivery = media.MediaDelivery(max_depth=2)
        first = delivery.ingest(media.pack_frame(_frame(payload=b"\x01\x02")))
        self.assertEqual(first.payload, b"\x01\x02")
        with self.assertRaises(media.ProtocolError) as caught:
            delivery.ingest(b"NOPE")
        self.assertEqual(caught.exception.code, "invalid_frame")
        self.assertEqual(delivery.depth(media.Direction.AUDIO_UPLINK), 1)
        delivery.ingest(media.pack_frame(_frame(sequence=1, timestamp_us=20_000)))
        self.assertEqual(delivery.take(media.Direction.AUDIO_UPLINK).sequence, 0)
        self.assertEqual(delivery.take(media.Direction.AUDIO_UPLINK).sequence, 1)

    def test_unknown_delivery_direction_is_a_frame_error(self):
        delivery = media.MediaDelivery()
        for invalid in (0, 3, True, "audio_uplink"):
            with self.subTest(value=invalid):
                with self.assertRaises(media.ProtocolError) as caught:
                    delivery.take(invalid)
                self.assertEqual(caught.exception.field, "direction")
                self.assertNotIn(str(invalid), str(caught.exception))


if __name__ == "__main__":
    unittest.main()

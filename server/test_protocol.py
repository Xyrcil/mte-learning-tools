import importlib.util
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path


_server_path = Path(__file__).resolve().with_name("server.py")
_spec = importlib.util.spec_from_file_location("local_game_server", _server_path)
assert _spec is not None and _spec.loader is not None
server = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = server
_spec.loader.exec_module(server)


class ProtocolTests(unittest.TestCase):
    def test_frame_round_trip(self) -> None:
        body = b"\x0a\x05guest\x12\x031.0"
        frame = server.encrypt_frame(0, body)
        self.assertEqual(struct.unpack(">I", frame[:4])[0], len(frame) - 4)
        command_id, decoded = server.decrypt_frame(frame[4:])
        self.assertEqual(command_id, 0)
        self.assertEqual(decoded, body)

    def test_queue_message_wire_format(self) -> None:
        body = server.make_server_ready("guest", "127.0.0.1", 19000)
        fields = server.parse_fields(body)
        self.assertEqual(server.first_string(fields, 1), "guest")
        self.assertEqual(server.first_string(fields, 2), "127.0.0.1")
        self.assertEqual(server.first_int(fields, 3), 19000)

    def test_battle_state_snapshots_and_followup_responses(self) -> None:
        hero_fields = server.parse_fields(server.make_hero_datas())
        self.assertEqual(len(hero_fields[1]), 1)
        phalanx_fields = server.parse_fields(server.make_phalanx_datas())
        self.assertEqual(server.first_int(phalanx_fields, 2), 0)
        self.assertEqual(len(phalanx_fields[1]), 5)
        self.assertEqual(len(phalanx_fields[5]), 5)
        first_auto = server.parse_fields(phalanx_fields[5][0])
        self.assertEqual(server.first_int(first_auto, 1), 0)
        self.assertEqual(len(first_auto[2]), 1)

        guide_request = server.field_varint(1, 42)
        guide_ret = server.parse_fields(
            server.make_unlock_teach_guide_ret(server.parse_fields(guide_request))
        )
        self.assertEqual(server.first_int(guide_ret, 2), 42)
        self.assertEqual(server.make_chosen_dungeon_ids_ret(), b"")

    def test_plot_read_over_returns_nested_completion_record(self) -> None:
        plot_data = b"".join(
            (
                server.field_varint(1, 123456),
                server.field_varint(3, 1),
            )
        )
        request = server.field_message(1, plot_data)
        response = server.parse_fields(
            server.make_read_plot_over_ret(server.parse_fields(request))
        )
        self.assertEqual(server.first_int(response, 1), 0)
        read_data = server.parse_fields(response[2][0])
        self.assertEqual(server.first_int(read_data, 1), 123456)
        self.assertEqual(server.first_int(read_data, 2), 1)
        self.assertEqual(server.first_int(read_data, 3), 1)

    def test_session_recorder_writes_raw_packet_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            recorder = server.SessionRecorder(Path(temporary))
            body = b"\x0a\x05guest"
            frame = server.encrypt_frame(30000, body)
            path = recorder.record("queue", "request", frame, 30000, body, ("127.0.0.1", 1234))

            self.assertEqual(path.name, "queue-request-30000.bin")
            self.assertEqual(path.read_bytes(), frame)
            entries = [json.loads(line) for line in recorder.metadata_path.read_text().splitlines()]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["command_id"], 30000)
            self.assertEqual(entries[0]["body_hex"], body.hex())


if __name__ == "__main__":
    unittest.main()

import unittest

from support import test_directory


class ArtifactTests(unittest.TestCase):
    def test_incremental_log_redacts_token_across_small_reads(self):
        from artifacts import log_page

        with test_directory() as root:
            path = root / "log.txt"
            path.write_bytes(b"line\nSECRET-TOKEN\nend\n")
            offset, seen = 0, ""
            while True:
                page = log_page(path, offset, 4, ("SECRET-TOKEN",))
                seen += page["text"]
                offset = page["next_offset"]
                if page["eof"]:
                    break
            self.assertEqual(seen, "line\n************\nend\n")

    def test_live_log_hides_partial_token_and_resumes_from_same_cursor(self):
        from artifacts import log_page

        with test_directory() as root:
            path = root / "log.txt"
            path.write_bytes(b"ok\nSECRET-")
            first = log_page(path, 0, 64, ("SECRET-TOKEN",), live=True)
            self.assertEqual(first["text"], "ok\n")
            self.assertEqual(first["next_offset"], 3)
            path.write_bytes(b"ok\nSECRET-TOKEN\n")
            page = log_page(path, 3, 64, ("SECRET-TOKEN",), live=True)
            self.assertEqual(page["text"], "************\n")

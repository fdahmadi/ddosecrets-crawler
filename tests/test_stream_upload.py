import asyncio
import io
import tempfile
import unittest
from pathlib import Path

import httpx

from ddosecrets_crawler.config import AppConfig
from ddosecrets_crawler.http_client import PoliteHttpClient
from ddosecrets_crawler.storage import (
    LocalMirrorStorage,
    MinioMirrorStorage,
    discard_http_staging_files,
    multipart_part_size,
)


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, data: bytes, chunk_size: int = 64 * 1024) -> None:
        self._data = data
        self._chunk_size = chunk_size

    async def __aiter__(self):
        step = self._chunk_size
        for index in range(0, len(self._data), step):
            yield self._data[index : index + step]


def _response(body: bytes, *, content_length: int | None = None) -> httpx.Response:
    length = len(body) if content_length is None else content_length
    return httpx.Response(
        200,
        headers={
            "content-type": "application/zip",
            "content-length": str(length),
            "etag": '"z"',
        },
        stream=_Chunks(body),
    )


def _client_for(handler) -> PoliteHttpClient:
    cfg = AppConfig()
    cfg.polite.min_delay = 0
    cfg.polite.max_delay = 0
    cfg.polite.burst_pause_every = 0
    client = PoliteHttpClient(cfg)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


class StreamUploadTest(unittest.TestCase):
    def test_part_size_stays_within_s3_limit(self) -> None:
        self.assertEqual(multipart_part_size(None), 64 * 1024 * 1024)
        self.assertGreaterEqual(multipart_part_size(1024), 8 * 1024 * 1024)
        huge = 100 * 1024**3
        part = multipart_part_size(huge)
        self.assertLessEqual((huge + part - 1) // part, 10_000)
        self.assertGreaterEqual(part, 8 * 1024 * 1024)

    def test_discard_keeps_torrent_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "torrents").mkdir()
            (root / "torrents" / "payload.bin").write_bytes(b"keep")
            (root / "data__archive.7z.part").write_bytes(b"partial")
            freed, count = discard_http_staging_files(root)
            self.assertEqual(count, 1)
            self.assertEqual(freed, len(b"partial"))
            self.assertTrue((root / "torrents" / "payload.bin").is_file())
            self.assertFalse((root / "data__archive.7z.part").exists())

    def test_streams_body_without_a_full_local_copy(self) -> None:
        payload = b"abc" * 200_000

        def handler(request: httpx.Request) -> httpx.Response:
            return _response(payload)

        async def run() -> None:
            client = _client_for(handler)
            with tempfile.TemporaryDirectory() as tmp:
                storage = LocalMirrorStorage(Path(tmp) / "mirror")
                try:
                    meta = await asyncio.wait_for(
                        client.download_to_storage(
                            "https://data.example/file.7z",
                            storage,
                            "data/data.example/file.7z",
                            expected_size=len(payload),
                        ),
                        timeout=10,
                    )
                finally:
                    await client.aclose()
                self.assertEqual(meta["bytes"], len(payload))
                self.assertEqual(
                    storage.get_bytes("data/data.example/file.7z"), payload
                )
                self.assertEqual(list(Path(tmp).rglob("*.part")), [])

        asyncio.run(run())

    def test_short_body_is_not_stored(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _response(b"short", content_length=100)

        async def run() -> None:
            client = _client_for(handler)
            with tempfile.TemporaryDirectory() as tmp:
                storage = LocalMirrorStorage(Path(tmp))
                key = "data/data.example/big.7z"
                with self.assertRaises(OSError):
                    await asyncio.wait_for(
                        client.download_to_storage(
                            "https://data.example/big.7z",
                            storage,
                            key,
                            expected_size=100,
                        ),
                        timeout=10,
                    )
                await client.aclose()
                self.assertFalse(storage.exists(key))
                self.assertEqual(list(Path(tmp).rglob("*.part")), [])

        asyncio.run(run())

    def test_minio_put_stream_reads_the_stream(self) -> None:
        seen: dict[str, object] = {}

        class FakeClient:
            def put_object(self, bucket, object_name, data, length, **kwargs):
                seen["bucket"] = bucket
                seen["object_name"] = object_name
                seen["length"] = length
                seen["body"] = data.read()
                seen["part_size"] = kwargs.get("part_size")
                seen["parallel"] = kwargs.get("num_parallel_uploads")

        store = MinioMirrorStorage.__new__(MinioMirrorStorage)
        store.client = FakeClient()
        store.bucket = "mirror"
        store.prefix = ""
        store.put_stream(
            "data/host/a.7z",
            io.BytesIO(b"hello"),
            length=5,
            content_type="application/zip",
        )
        self.assertEqual(seen["body"], b"hello")
        self.assertEqual(seen["length"], 5)
        self.assertEqual(seen["object_name"], "data/host/a.7z")
        self.assertEqual(seen["parallel"], 1)
        self.assertGreaterEqual(int(seen["part_size"]), 8 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()

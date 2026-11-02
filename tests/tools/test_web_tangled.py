"""The Tangled adapter of ``read_url``: code pages as raw files."""

import httpx

from tests.support.routes import Routes
from yggdrasil.tools import web
from yggdrasil.tools.web import API_USER_AGENT, Extracted


async def test_tangled_blob_reads_the_raw_file_with_its_lines() -> None:
    code = 'fn main() {\n    println!("hi");\n}\n'

    def raw(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == API_USER_AGENT  # browser UAs without a browser TLS stack get 403
        return httpx.Response(200, text=code, headers={"content-type": "text/plain; charset=utf-8"})

    routes = Routes({"https://tangled.org/alice.example.com/repo/raw/main/src/main.rs": raw})
    async with routes.client() as client:
        result = await web._tangled_blob(
            client, httpx.URL("https://tangled.org/alice.example.com/repo/blob/main/src/main.rs")
        )
        other = await web._tangled_blob(client, httpx.URL("https://tangled.org/alice.example.com/repo/issues/1"))
    assert isinstance(result, Extracted)
    assert (result.text, result.via) == (code.strip(), "tangled")
    assert result.title == "alice.example.com/repo: src/main.rs"
    assert other is None

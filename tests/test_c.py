# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import ast
import glob
import os.path

import pretend

import pytest

import http11.c


def _load_cases():
    cases = glob.iglob(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "cases",
            "*.py",
        )
    )

    for casefile in cases:
        with open(casefile) as fp:
            for case in ast.literal_eval(fp.read()):
                yield case["message"], case["expected"]


def test_runtime_compile_fails():
    ffi = pretend.stub(
        verifier=pretend.stub(
            compile_module=lambda *a, **k: None,
            _compile_module=lambda *a, **k: None,
        ),
    )
    lib = http11.c.Library(ffi)

    with pytest.raises(RuntimeError):
        lib.ffi.verifier.compile_module()

    with pytest.raises(RuntimeError):
        lib.ffi.verifier._compile_module()


@pytest.mark.parametrize(("message", "expected"), _load_cases())
def test_parsing(parser, data, message, expected):
    if not isinstance(message, list):
        message = [message]

    for chunk in message:
        parsed = http11.c.lib.HTTPParser_execute(parser, chunk, 0, len(chunk))
        assert parsed == len(chunk)

    assert data == expected
    assert parser.finished
    assert not parser.error

    if "request_method" in expected:
        assert parser.type == http11.c.lib.REQUEST
    else:
        assert parser.type == http11.c.lib.RESPONSE


@pytest.mark.parametrize(("message", "expected"), _load_cases())
def test_number_callbacks(message, expected):
    if not isinstance(message, list):
        message = [message]

    class CallRecorder(object):

        def __init__(self):
            self.calls = 0

        def __call__(self, *args, **kwargs):
            self.calls += 1
            return 0

    parser = http11.c.lib.HTTPParser_create()

    request_method = CallRecorder()
    parser.request_method = c1 = http11.c.ffi.callback(
        "int(const char *value, size_t length)",
        request_method,
    )

    request_uri = CallRecorder()
    parser.request_uri = c2 = http11.c.ffi.callback(
        "int(const char *value, size_t length)",
        request_uri,
    )

    http_version = CallRecorder()
    parser.http_version = c3 = http11.c.ffi.callback(
        "int(const char *value, size_t length)",
        http_version,
    )

    reason_phrase = CallRecorder()
    parser.reason_phrase = c4 = http11.c.ffi.callback(
        "int(const char *value, size_t length)",
        reason_phrase,
    )

    status_code = CallRecorder()
    parser.status_code = c5 = http11.c.ffi.callback(
        "int(const unsigned short)",
        status_code,
    )

    http_header = CallRecorder()
    parser.http_header = c6 = http11.c.ffi.callback(
        "int(const char *name, size_t namelen, const char *value, "
        "size_t valuelen)",
        http_header,
    )

    http11.c.lib.HTTPParser_init(parser)

    for chunk in message:
        parsed = http11.c.lib.HTTPParser_execute(parser, chunk, 0, len(chunk))
        assert parsed == len(chunk)

    http11.c.lib.HTTPParser_destroy(parser)

    assert request_method.calls == (1 if "request_method" in expected else 0)
    assert request_uri.calls == (1 if "request_uri" in expected else 0)
    assert http_version.calls == 1
    assert reason_phrase.calls == (1 if "reason_phrase" in expected else 0)
    assert status_code.calls == (1 if "status_code" in expected else 0)
    assert http_header.calls == sum(
        len(v) for v in (
            expected["headers"].values() if "headers" in expected else []
        )
    )

    # Quick hack to prevent pep8 linting from saying these variables are
    # unused.
    c1, c2, c3, c4, c5, c6


def test_offset_length(parser, data):
    msg = b"GET / HTTP/1.1\r\nFoo: Bar\r\n\r\n"

    assert http11.c.lib.HTTPParser_execute(parser, msg, 0, 16) == 16
    assert http11.c.lib.HTTPParser_execute(parser, msg, 16, 20) == 4
    assert http11.c.lib.HTTPParser_execute(parser, msg, 20, 28) == 8

    assert data == {
        "request_method": b"GET",
        "request_uri": b"/",
        "http_version": b"HTTP/1.1",
        "headers": {
            b"Foo": [b"Bar"],
        }
    }
    assert parser.finished
    assert not parser.error


def test_doesnt_read_past_end(parser, data):
    msg = b"GET / HTTP/1.1\r\nFoo: Bar\r\n\r\nThis data should not be read."

    assert http11.c.lib.HTTPParser_execute(parser, msg, 0, len(msg)) == 28

    assert data == {
        "request_method": b"GET",
        "request_uri": b"/",
        "http_version": b"HTTP/1.1",
        "headers": {
            b"Foo": [b"Bar"],
        }
    }
    assert parser.finished
    assert not parser.error


def test_http_version_error(parser, data):
    msg = b"GET / HTTP/2.0\r\n\r\n"

    http11.c.lib.HTTPParser_execute(parser, msg, 0, len(msg))

    assert parser.finished
    assert parser.error == http11.c.lib.EBADVERSION


def test_eof_handling(parser):
    msg = b"HTTP/1.2 200 OK\r\nFoo: Bar\r\n"

    http11.c.lib.HTTPParser_execute(parser, msg, 0, len(msg))
    http11.c.lib.HTTPParser_execute(parser, http11.c.ffi.NULL, 0, 0)

    assert parser.finished
    assert parser.error == http11.c.lib.EEOF


def test_leading_crlf(parser, data):
    """
    In the interest of robustness, a server that is expecting to receive and
    parse a request-line SHOULD ignore at least one empty line (CRLF) received
    prior to the request-line.
    """
    msg = b"\r\n\r\n\r\n\r\nGET / HTTP/1.1\r\n\r\n"

    http11.c.lib.HTTPParser_execute(parser, msg, 0, len(msg))

    assert data == {
        "request_method": b"GET",
        "request_uri": b"/",
        "http_version": b"HTTP/1.1",
    }
    assert parser.finished
    assert not parser.error

    msg = b"\r\n\r\n\r\n\r\nHTTP/1.1 200 OK\r\n\r\n"

    http11.c.lib.HTTPParser_execute(parser, msg, 0, len(msg))

    assert parser.finished
    assert parser.error == http11.c.lib.EINVALIDMSG

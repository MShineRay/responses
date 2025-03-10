import inspect
import json as json_module
import logging
from collections import namedtuple
from collections.abc import Sequence
from collections.abc import Sized
from functools import wraps
from http import client
from itertools import groupby
from re import Pattern
from threading import Lock as _ThreadingLock
from typing import TYPE_CHECKING
from typing import Any
from typing import Callable
from typing import Dict
from typing import Iterable
from typing import Iterator
from typing import List
from typing import Mapping
from typing import Optional
from typing import Tuple
from typing import Type
from typing import Union
from typing import overload
from warnings import warn

from requests.adapters import HTTPAdapter
from requests.adapters import MaxRetryError
from requests.exceptions import ConnectionError

from responses.matchers import json_params_matcher as _json_params_matcher
from responses.matchers import query_string_matcher as _query_string_matcher
from responses.matchers import urlencoded_params_matcher as _urlencoded_params_matcher
from responses.registries import FirstMatchRegistry

try:
    from typing_extensions import Literal
except ImportError:  # pragma: no cover
    from typing import Literal  # type: ignore  # pragma: no cover

try:
    from requests.packages.urllib3.response import HTTPResponse
except ImportError:  # pragma: no cover
    from urllib3.response import HTTPResponse  # pragma: no cover

try:
    from requests.packages.urllib3.connection import HTTPHeaderDict
except ImportError:  # pragma: no cover
    from urllib3.response import HTTPHeaderDict  # type: ignore[attr-defined]
try:
    from requests.packages.urllib3.util.url import parse_url
except ImportError:  # pragma: no cover
    from urllib3.util.url import parse_url  # pragma: no cover

from io import BufferedReader
from io import BytesIO
from unittest import mock as std_mock
from urllib.parse import parse_qsl
from urllib.parse import quote
from urllib.parse import urlsplit
from urllib.parse import urlunparse
from urllib.parse import urlunsplit

if TYPE_CHECKING:  # pragma: no cover
    # import only for linter run
    from mypy.typeshed.stdlib.unittest.mock import _patcher as _mock_patcher
    from requests import PreparedRequest
    from requests import models
    from urllib3 import Retry as _Retry

# Block of type annotations
_Body = Union[str, BaseException, "Response", BufferedReader, bytes, None]
_F = Callable[..., Any]
_HeaderSet = Optional[Union[Mapping[str, str], List[Tuple[str, str]]]]
_MatcherIterable = Iterable[Callable[..., Tuple[bool, str]]]
_HTTPMethodOrResponse = Optional[Union[str, "BaseResponse"]]
_URLPatternType = Union["Pattern[str]", str]

Call = namedtuple("Call", ["request", "response"])
_real_send = HTTPAdapter.send
_UNSET = object()

logger = logging.getLogger("responses")


class FalseBool:
    """Class to mock up built-in False boolean.

    Used for backwards compatibility, see
    https://github.com/getsentry/responses/issues/464
    """

    def __bool__(self) -> bool:
        return False

    __nonzero__ = __bool__


def urlencoded_params_matcher(params: Optional[Dict[str, str]]) -> Callable[..., Any]:
    warn(
        "Function is deprecated. Use 'from responses.matchers import urlencoded_params_matcher'",
        DeprecationWarning,
    )
    return _urlencoded_params_matcher(params)


def json_params_matcher(params: Optional[Dict[str, Any]]) -> Callable[..., Any]:
    warn(
        "Function is deprecated. Use 'from responses.matchers import json_params_matcher'",
        DeprecationWarning,
    )
    return _json_params_matcher(params)


def _has_unicode(s: str) -> bool:
    return any(ord(char) > 128 for char in s)


def _clean_unicode(url: str) -> str:
    """Clean up URLs, which use punycode to handle unicode chars.

    Applies percent encoding to URL path and query if required.

    Parameters
    ----------
    url : str
        URL that should be cleaned from unicode

    Returns
    -------
    str
        Cleaned URL

    """
    urllist = list(urlsplit(url))
    netloc = urllist[1]
    if _has_unicode(netloc):
        domains = netloc.split(".")
        for i, d in enumerate(domains):
            if _has_unicode(d):
                d = "xn--" + d.encode("punycode").decode("ascii")
                domains[i] = d
        urllist[1] = ".".join(domains)
        url = urlunsplit(urllist)

    # Clean up path/query/params, which use url-encoding to handle unicode chars
    chars = list(url)
    for i, x in enumerate(chars):
        if ord(x) > 128:
            chars[i] = quote(x)

    return "".join(chars)


def get_wrapped(
    func: Callable[..., Any],
    responses: "RequestsMock",
    *,
    registry: Optional[Any] = None,
    assert_all_requests_are_fired: Optional[bool] = None,
) -> Callable[..., Any]:
    """Wrap provided function inside ``responses`` context manager.

    Provides a synchronous or asynchronous wrapper for the function.


    Parameters
    ----------
    func : Callable
        Function to wrap.
    responses : RequestsMock
        Mock object that is used as context manager.
    registry : FirstMatchRegistry, optional
        Custom registry that should be applied. See ``responses.registries``
    assert_all_requests_are_fired : bool
        Raise an error if not all registered responses were executed.

    Returns
    -------
    Callable
        Wrapped function

    """
    assert_mock = std_mock.patch.object(
        target=responses,
        attribute="assert_all_requests_are_fired",
        new=assert_all_requests_are_fired,
    )

    if inspect.iscoroutinefunction(func):
        # set asynchronous wrapper if requestor function is asynchronous
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]

            if registry is not None:
                responses._set_registry(registry)

            with assert_mock, responses:
                return await func(*args, **kwargs)

    else:

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]

            if registry is not None:
                responses._set_registry(registry)

            with assert_mock, responses:
                # set 'assert_all_requests_are_fired' temporarily for a single run.
                # Mock automatically unsets to avoid leakage to another decorated
                # function since we still apply the value on 'responses.mock' object
                return func(*args, **kwargs)

    return wrapper


class CallList(Sequence, Sized):  # type: ignore[type-arg]
    def __init__(self) -> None:
        self._calls: List[Call] = []

    def __iter__(self) -> Iterator[Call]:
        return iter(self._calls)

    def __len__(self) -> int:
        return len(self._calls)

    def __getitem__(self, idx: int) -> Call:  # type: ignore[override]
        return self._calls[idx]

    def add(self, request: "PreparedRequest", response: _Body) -> None:
        self._calls.append(Call(request, response))

    def reset(self) -> None:
        self._calls = []


def _ensure_url_default_path(
    url: _URLPatternType,
) -> _URLPatternType:
    """Add empty URL path '/' if doesn't exist.

    Examples
    --------
    >>> _ensure_url_default_path("http://example.com")
    "http://example.com/"

    Parameters
    ----------
    url : str or re.Pattern
        URL to validate.

    Returns
    -------
    url : str or re.Pattern
        Modified URL if str or unchanged re.Pattern

    """
    if isinstance(url, str):
        url_parts = list(urlsplit(url))
        if url_parts[2] == "":
            url_parts[2] = "/"
            url = urlunsplit(url_parts)
    return url


def _get_url_and_path(url: str) -> str:
    """Construct URL only containing scheme, netloc and path by truncating other parts.

    This method complies with RFC 3986.

    Examples
    --------
    >>> _get_url_and_path("http://example.com/path;segment?ab=xy&zed=qwe#test=1&foo=bar")
    "http://example.com/path;segment"


    Parameters
    ----------
    url : str
        URL to parse.

    Returns
    -------
    url : str
        URL with scheme, netloc and path

    """
    url_parsed = urlsplit(url)
    url_and_path = urlunparse(
        [url_parsed.scheme, url_parsed.netloc, url_parsed.path, None, None, None]
    )
    return parse_url(url_and_path).url


def _handle_body(
    body: Optional[Union[bytes, BufferedReader, str]]
) -> Union[BufferedReader, BytesIO]:
    """Generates `Response` body.

    Parameters
    ----------
    body : str or bytes or BufferedReader
        Input data to generate `Response` body.

    Returns
    -------
    body : BufferedReader or BytesIO
        `Response` body

    """
    if isinstance(body, str):
        body = body.encode("utf-8")
    if isinstance(body, BufferedReader):
        return body

    data = BytesIO(body)  # type: ignore[arg-type]

    def is_closed() -> bool:
        """
        Real Response uses HTTPResponse as body object.
        Thus, when method is_closed is called first to check if there is any more
        content to consume and the file-like object is still opened

        This method ensures stability to work for both:
        https://github.com/getsentry/responses/issues/438
        https://github.com/getsentry/responses/issues/394

        where file should be intentionally be left opened to continue consumption
        """
        if not data.closed and data.read(1):
            # if there is more bytes to read then keep open, but return pointer
            data.seek(-1, 1)
            return False
        else:
            if not data.closed:
                # close but return False to mock like is still opened
                data.close()
                return False

            # only if file really closed (by us) return True
            return True

    data.isclosed = is_closed  # type: ignore[attr-defined]
    return data


class BaseResponse(object):
    passthrough: bool = False
    content_type: Optional[str] = None
    headers: Optional[Mapping[str, str]] = None
    stream: Optional[bool] = False

    def __init__(
        self,
        method: str,
        url: _URLPatternType,
        match_querystring: Union[bool, object] = None,
        match: "_MatcherIterable" = (),
        *,
        passthrough: bool = False,
    ) -> None:
        self.method: str = method
        # ensure the url has a default path set if the url is a string
        self.url: _URLPatternType = _ensure_url_default_path(url)

        if self._should_match_querystring(match_querystring):
            match = tuple(match) + (
                _query_string_matcher(urlsplit(self.url).query),  # type: ignore[arg-type]
            )

        self.match: "_MatcherIterable" = match
        self.call_count: int = 0
        self.passthrough = passthrough

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, BaseResponse):
            return False

        if self.method != other.method:
            return False

        # Can't simply do an equality check on the objects directly here since __eq__ isn't
        # implemented for regex. It might seem to work as regex is using a cache to return
        # the same regex instances, but it doesn't in all cases.
        self_url = self.url.pattern if isinstance(self.url, Pattern) else self.url
        other_url = other.url.pattern if isinstance(other.url, Pattern) else other.url

        return self_url == other_url

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    def _should_match_querystring(
        self, match_querystring_argument: Union[bool, object]
    ) -> Union[bool, object]:
        if isinstance(self.url, Pattern):
            # the old default from <= 0.9.0
            return False

        if match_querystring_argument is not None:
            if not isinstance(match_querystring_argument, FalseBool):
                warn(
                    (
                        "Argument 'match_querystring' is deprecated. "
                        "Use 'responses.matchers.query_param_matcher' or "
                        "'responses.matchers.query_string_matcher'"
                    ),
                    DeprecationWarning,
                )
            return match_querystring_argument

        return bool(urlsplit(self.url).query)

    def _url_matches(self, url: _URLPatternType, other: str) -> bool:
        if isinstance(url, str):
            if _has_unicode(url):
                url = _clean_unicode(url)

            return _get_url_and_path(url) == _get_url_and_path(other)

        elif isinstance(url, Pattern) and url.match(other):
            return True

        else:
            return False

    @staticmethod
    def _req_attr_matches(
        match: "_MatcherIterable", request: "PreparedRequest"
    ) -> Tuple[bool, str]:
        for matcher in match:
            valid, reason = matcher(request)
            if not valid:
                return False, reason

        return True, ""

    def get_headers(self) -> HTTPHeaderDict:
        headers = HTTPHeaderDict()  # Duplicate headers are legal
        if self.content_type is not None:
            headers["Content-Type"] = self.content_type
        if self.headers:
            headers.extend(self.headers)
        return headers

    def get_response(self, request: "PreparedRequest") -> HTTPResponse:
        raise NotImplementedError

    def matches(self, request: "PreparedRequest") -> Tuple[bool, str]:
        if request.method != self.method:
            return False, "Method does not match"

        if not self._url_matches(self.url, str(request.url)):
            return False, "URL does not match"

        valid, reason = self._req_attr_matches(self.match, request)
        if not valid:
            return False, reason

        return True, ""


class Response(BaseResponse):
    def __init__(
        self,
        method: str,
        url: _URLPatternType,
        body: _Body = "",
        json: Optional[Any] = None,
        status: int = 200,
        headers: Optional[Mapping[str, str]] = None,
        stream: Optional[bool] = None,
        content_type: Union[str, object] = _UNSET,
        auto_calculate_content_length: bool = False,
        **kwargs: Any,
    ) -> None:
        # if we were passed a `json` argument,
        # override the body and content_type
        if json is not None:
            assert not body
            body = json_module.dumps(json)
            if content_type is _UNSET:
                content_type = "application/json"

        if content_type is _UNSET:
            if isinstance(body, str) and _has_unicode(body):
                content_type = "text/plain; charset=utf-8"
            else:
                content_type = "text/plain"

        self.body: _Body = body
        self.status: int = status
        self.headers: Optional[Mapping[str, str]] = headers

        if stream is not None:
            warn(
                "stream argument is deprecated. Use stream parameter in request directly",
                DeprecationWarning,
            )

        self.stream: Optional[bool] = stream
        self.content_type: str = content_type  # type: ignore[assignment]
        self.auto_calculate_content_length: bool = auto_calculate_content_length
        super().__init__(method, url, **kwargs)

    def get_response(self, request: "PreparedRequest") -> HTTPResponse:
        if self.body and isinstance(self.body, Exception):
            raise self.body

        headers = self.get_headers()
        status = self.status

        assert not isinstance(self.body, (Response, BaseException))
        body = _handle_body(self.body)

        if (
            self.auto_calculate_content_length
            and isinstance(body, BytesIO)
            and "Content-Length" not in headers
        ):
            content_length = len(body.getvalue())
            headers["Content-Length"] = str(content_length)

        return HTTPResponse(
            status=status,
            reason=client.responses.get(status, None),
            body=body,
            headers=headers,
            original_response=OriginalResponseShim(headers),
            preload_content=False,
        )

    def __repr__(self) -> str:
        return (
            "<Response(url='{url}' status={status} "
            "content_type='{content_type}' headers='{headers}')>".format(
                url=self.url,
                status=self.status,
                content_type=self.content_type,
                headers=json_module.dumps(self.headers),
            )
        )


class CallbackResponse(BaseResponse):
    def __init__(
        self,
        method: str,
        url: _URLPatternType,
        callback: Callable[[Any], Any],
        stream: Optional[bool] = None,
        content_type: Optional[str] = "text/plain",
        **kwargs: Any,
    ) -> None:
        self.callback = callback

        if stream is not None:
            warn(
                "stream argument is deprecated. Use stream parameter in request directly",
                DeprecationWarning,
            )
        self.stream: Optional[bool] = stream
        self.content_type: Optional[str] = content_type
        super().__init__(method, url, **kwargs)

    def get_response(self, request: "PreparedRequest") -> HTTPResponse:
        headers = self.get_headers()

        result = self.callback(request)
        if isinstance(result, Exception):
            raise result

        status, r_headers, body = result
        if isinstance(body, Exception):
            raise body

        # If the callback set a content-type remove the one
        # set in add_callback() so that we don't have multiple
        # content type values.
        has_content_type = False
        if isinstance(r_headers, dict) and "Content-Type" in r_headers:
            has_content_type = True
        elif isinstance(r_headers, list):
            has_content_type = any(
                [h for h in r_headers if h and h[0].lower() == "content-type"]
            )
        if has_content_type:
            headers.pop("Content-Type", None)

        body = _handle_body(body)
        headers.extend(r_headers)

        return HTTPResponse(
            status=status,
            reason=client.responses.get(status, None),
            body=body,
            headers=headers,
            original_response=OriginalResponseShim(headers),
            preload_content=False,
        )


class PassthroughResponse(BaseResponse):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, passthrough=True, **kwargs)


class OriginalResponseShim(object):
    """
    Shim for compatibility with older versions of urllib3

    requests cookie handling depends on responses having a property chain of
    `response._original_response.msg` which contains the response headers [1]

    Using HTTPResponse() for this purpose causes compatibility errors with
    urllib3<1.23.0. To avoid adding more dependencies we can use this shim.

    [1]: https://github.com/psf/requests/blob/75bdc998e2d/requests/cookies.py#L125
    """

    def __init__(self, headers: Any) -> None:
        self.msg: Any = headers

    def isclosed(self) -> bool:
        return True

    def close(self) -> None:
        return


class RequestsMock(object):
    DELETE: Literal["DELETE"] = "DELETE"
    GET: Literal["GET"] = "GET"
    HEAD: Literal["HEAD"] = "HEAD"
    OPTIONS: Literal["OPTIONS"] = "OPTIONS"
    PATCH: Literal["PATCH"] = "PATCH"
    POST: Literal["POST"] = "POST"
    PUT: Literal["PUT"] = "PUT"

    response_callback: Optional[Callable[[Any], Any]] = None

    def __init__(
        self,
        assert_all_requests_are_fired: bool = True,
        response_callback: Optional[Callable[[Any], Any]] = None,
        passthru_prefixes: Tuple[str, ...] = (),
        target: str = "requests.adapters.HTTPAdapter.send",
        registry: Type[FirstMatchRegistry] = FirstMatchRegistry,
    ):
        self._calls: CallList = CallList()
        self.reset()
        self._registry: FirstMatchRegistry = registry()  # call only after reset
        self.assert_all_requests_are_fired: bool = assert_all_requests_are_fired
        self.response_callback: Optional[Callable[[Any], Response]] = response_callback
        self.passthru_prefixes: Tuple[_URLPatternType, ...] = tuple(passthru_prefixes)
        self.target: str = target
        self._patcher: Optional["_mock_patcher"] = None
        self._thread_lock = _ThreadingLock()

    def get_registry(self) -> FirstMatchRegistry:
        return self._registry

    def _set_registry(self, new_registry: Type[FirstMatchRegistry]) -> None:
        if self.registered():
            err_msg = (
                "Cannot replace Registry, current registry has responses.\n"
                "Run 'responses.registry.reset()' first"
            )
            raise AttributeError(err_msg)

        self._registry = new_registry()

    def reset(self) -> None:
        self._registry = FirstMatchRegistry()
        self._calls.reset()
        self.passthru_prefixes = ()

    def add(
        self,
        method: _HTTPMethodOrResponse = None,
        url: "Optional[_URLPatternType]" = None,
        body: _Body = "",
        adding_headers: _HeaderSet = None,
        *args: Any,
        **kwargs: Any,
    ) -> BaseResponse:
        """
        >>> import responses

        A basic request:
        >>> responses.add(responses.GET, 'http://example.com')

        You can also directly pass an object which implements the
        ``BaseResponse`` interface:

        >>> responses.add(Response(...))

        A JSON payload:

        >>> responses.add(
        >>>     method='GET',
        >>>     url='http://example.com',
        >>>     json={'foo': 'bar'},
        >>> )

        Custom headers:

        >>> responses.add(
        >>>     method='GET',
        >>>     url='http://example.com',
        >>>     headers={'X-Header': 'foo'},
        >>> )

        """
        if isinstance(method, BaseResponse):
            return self._registry.add(method)

        if adding_headers is not None:
            kwargs.setdefault("headers", adding_headers)
        if "content_type" in kwargs and "headers" in kwargs:
            header_keys = [header.lower() for header in kwargs["headers"]]
            if "content-type" in header_keys:
                raise RuntimeError(
                    "You cannot define both `content_type` and `headers[Content-Type]`."
                    " Using the `content_type` kwarg is recommended."
                )

        assert url is not None
        assert isinstance(method, str)
        response = Response(method=method, url=url, body=body, **kwargs)
        return self._registry.add(response)

    def delete(self, *args: Any, **kwargs: Any) -> BaseResponse:
        return self.add(DELETE, *args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> BaseResponse:
        return self.add(GET, *args, **kwargs)

    def head(self, *args: Any, **kwargs: Any) -> BaseResponse:
        return self.add(HEAD, *args, **kwargs)

    def options(self, *args: Any, **kwargs: Any) -> BaseResponse:
        return self.add(OPTIONS, *args, **kwargs)

    def patch(self, *args: Any, **kwargs: Any) -> BaseResponse:
        return self.add(PATCH, *args, **kwargs)

    def post(self, *args: Any, **kwargs: Any) -> BaseResponse:
        return self.add(POST, *args, **kwargs)

    def put(self, *args: Any, **kwargs: Any) -> BaseResponse:
        return self.add(PUT, *args, **kwargs)

    def add_passthru(self, prefix: _URLPatternType) -> None:
        """
        Register a URL prefix or regex to passthru any non-matching mock requests to.

        For example, to allow any request to 'https://example.com', but require
        mocks for the remainder, you would add the prefix as so:

        >>> import responses
        >>> responses.add_passthru('https://example.com')

        Regex can be used like:

        >>> import re
        >>> responses.add_passthru(re.compile('https://example.com/\\w+'))
        """
        if not isinstance(prefix, Pattern) and _has_unicode(prefix):
            prefix = _clean_unicode(prefix)
        self.passthru_prefixes += (prefix,)

    def remove(
        self,
        method_or_response: _HTTPMethodOrResponse = None,
        url: "Optional[_URLPatternType]" = None,
    ) -> List[BaseResponse]:
        """
        Removes a response previously added using ``add()``, identified
        either by a response object inheriting ``BaseResponse`` or
        ``method`` and ``url``. Removes all matching responses.

        >>> import responses
        >>> responses.add(responses.GET, 'http://example.org')
        >>> responses.remove(responses.GET, 'http://example.org')
        """
        if isinstance(method_or_response, BaseResponse):
            response = method_or_response
        else:
            assert url is not None
            assert isinstance(method_or_response, str)
            response = BaseResponse(method=method_or_response, url=url)

        return self._registry.remove(response)

    def replace(
        self,
        method_or_response: _HTTPMethodOrResponse = None,
        url: "Optional[_URLPatternType]" = None,
        body: _Body = "",
        *args: Any,
        **kwargs: Any,
    ) -> BaseResponse:
        """
        Replaces a response previously added using ``add()``. The signature
        is identical to ``add()``. The response is identified using ``method``
        and ``url``, and the first matching response is replaced.

        >>> import responses
        >>> responses.add(responses.GET, 'http://example.org', json={'data': 1})
        >>> responses.replace(responses.GET, 'http://example.org', json={'data': 2})
        """
        if isinstance(method_or_response, BaseResponse):
            response = method_or_response
        else:
            assert url is not None
            assert isinstance(method_or_response, str)
            response = Response(method=method_or_response, url=url, body=body, **kwargs)

        return self._registry.replace(response)

    def upsert(
        self,
        method_or_response: _HTTPMethodOrResponse = None,
        url: "Optional[_URLPatternType]" = None,
        body: _Body = "",
        *args: Any,
        **kwargs: Any,
    ) -> BaseResponse:
        """
        Replaces a response previously added using ``add()``, or adds the response
        if no response exists.  Responses are matched using ``method``and ``url``.
        The first matching response is replaced.

        >>> import responses
        >>> responses.add(responses.GET, 'http://example.org', json={'data': 1})
        >>> responses.upsert(responses.GET, 'http://example.org', json={'data': 2})
        """
        try:
            return self.replace(method_or_response, url, body, *args, **kwargs)
        except ValueError:
            return self.add(method_or_response, url, body, *args, **kwargs)

    def add_callback(
        self,
        method: str,
        url: _URLPatternType,
        callback: Callable[
            ["PreparedRequest"], Union[Exception, Tuple[int, Mapping[str, str], _Body]]
        ],
        match_querystring: Union[bool, FalseBool] = FalseBool(),
        content_type: Optional[str] = "text/plain",
        match: "_MatcherIterable" = (),
    ) -> None:

        self._registry.add(
            CallbackResponse(
                url=url,
                method=method,
                callback=callback,
                content_type=content_type,
                match_querystring=match_querystring,
                match=match,
            )
        )

    def registered(self) -> List["BaseResponse"]:
        return self._registry.registered

    @property
    def calls(self) -> CallList:
        return self._calls

    def __enter__(self) -> "RequestsMock":
        self.start()
        return self

    def __exit__(self, type: Any, value: Any, traceback: Any) -> bool:
        success = type is None
        self.stop(allow_assert=success)
        self.reset()
        return success

    @overload
    def activate(self, func: _F = ...) -> _F:
        """Overload for scenario when 'responses.activate' is used."""
        ...  # pragma: no cover

    @overload
    def activate(
        self,
        *,
        registry: Type[Any] = ...,
        assert_all_requests_are_fired: bool = ...,
    ) -> Callable[["_F"], "_F"]:
        """Overload for scenario when
        'responses.activate(registry=, assert_all_requests_are_fired=True)' is used.

        See https://github.com/getsentry/responses/pull/469 for more details

        """
        ...  # pragma: no cover

    def activate(
        self,
        func: Optional[_F] = None,
        *,
        registry: Optional[Type[Any]] = None,
        assert_all_requests_are_fired: bool = False,
    ) -> Union[Callable[["_F"], "_F"], _F]:
        if func is not None:
            return get_wrapped(func, self)

        def deco_activate(function: _F) -> Callable[..., Any]:
            return get_wrapped(
                function,
                self,
                registry=registry,
                assert_all_requests_are_fired=assert_all_requests_are_fired,
            )

        return deco_activate

    def _find_match(
        self, request: "PreparedRequest"
    ) -> Tuple[Optional["BaseResponse"], List[str]]:
        """
        Iterates through all available matches and validates if any of them matches the request

        :param request: (PreparedRequest), request object
        :return:
            (Response) found match. If multiple found, then remove & return the first match.
            (list) list with reasons why other matches don't match
        """
        with self._thread_lock:
            return self._registry.find(request)

    def _parse_request_params(
        self, url: str
    ) -> Dict[str, Union[str, int, float, List[Optional[Union[str, int, float]]]]]:
        params: Dict[str, Union[str, int, float, List[Any]]] = {}
        for key, val in groupby(parse_qsl(urlsplit(url).query), lambda kv: kv[0]):
            values = list(map(lambda x: x[1], val))
            if len(values) == 1:
                values = values[0]  # type: ignore[assignment]
            params[key] = values
        return params

    def _on_request(
        self,
        adapter: "HTTPAdapter",
        request: "PreparedRequest",
        *,
        retries: Optional["_Retry"] = None,
        **kwargs: Any,
    ) -> "models.Response":
        # add attributes params and req_kwargs to 'request' object for further match comparison
        # original request object does not have these attributes
        request.params = self._parse_request_params(request.path_url)  # type: ignore[attr-defined]
        request.req_kwargs = kwargs  # type: ignore[attr-defined]
        request_url = str(request.url)

        match, match_failed_reasons = self._find_match(request)
        resp_callback = self.response_callback

        if match is None:
            if any(
                [
                    p.match(request_url)
                    if isinstance(p, Pattern)
                    else request_url.startswith(p)
                    for p in self.passthru_prefixes
                ]
            ):
                logger.info("request.allowed-passthru", extra={"url": request_url})
                return _real_send(adapter, request, **kwargs)

            error_msg = (
                "Connection refused by Responses - the call doesn't "
                "match any registered mock.\n\n"
                "Request: \n"
                f"- {request.method} {request_url}\n\n"
                "Available matches:\n"
            )
            for i, m in enumerate(self.registered()):
                error_msg += "- {} {} {}\n".format(
                    m.method, m.url, match_failed_reasons[i]
                )

            if self.passthru_prefixes:
                error_msg += "Passthru prefixes:\n"
                for p in self.passthru_prefixes:
                    error_msg += "- {}\n".format(p)

            response = ConnectionError(error_msg)
            response.request = request

            self._calls.add(request, response)
            raise response

        if match.passthrough:
            logger.info("request.passthrough-response", extra={"url": request_url})
            response = _real_send(adapter, request, **kwargs)  # type: ignore[assignment]
        else:
            try:
                response = adapter.build_response(  # type: ignore[no-untyped-call]
                    request, match.get_response(request)
                )
            except BaseException as response:
                match.call_count += 1
                self._calls.add(request, response)
                raise

        if resp_callback:
            response = resp_callback(response)  # type: ignore[misc]
        match.call_count += 1
        self._calls.add(request, response)  # type: ignore[misc]

        retries = retries or adapter.max_retries
        # first validate that current request is eligible to be retried.
        # See ``requests.packages.urllib3.util.retry.Retry`` documentation.
        if retries.is_retry(
            method=response.request.method, status_code=response.status_code  # type: ignore[misc]
        ):
            try:
                retries = retries.increment(
                    method=response.request.method,  # type: ignore[misc]
                    url=response.url,  # type: ignore[misc]
                    response=response,  # type: ignore[misc]
                )
                return self._on_request(adapter, request, retries=retries, **kwargs)
            except MaxRetryError:
                if retries.raise_on_status:
                    raise
                return response
        return response

    def start(self) -> None:
        if self._patcher:
            # we must not override value of the _patcher if already applied
            # this prevents issues when one decorated function is called from
            # another decorated function
            return

        def unbound_on_send(
            adapter: "HTTPAdapter", request: "PreparedRequest", *a: Any, **kwargs: Any
        ) -> "models.Response":
            return self._on_request(adapter, request, *a, **kwargs)

        self._patcher = std_mock.patch(target=self.target, new=unbound_on_send)
        self._patcher.start()

    def stop(self, allow_assert: bool = True) -> None:
        if self._patcher:
            # prevent stopping unstarted patchers
            self._patcher.stop()

            # once patcher is stopped, clean it. This is required to create a new
            # fresh patcher on self.start()
            self._patcher = None

        if not self.assert_all_requests_are_fired:
            return

        if not allow_assert:
            return

        not_called = [m for m in self.registered() if m.call_count == 0]
        if not_called:
            raise AssertionError(
                "Not all requests have been executed {0!r}".format(
                    [(match.method, match.url) for match in not_called]
                )
            )

    def assert_call_count(self, url: str, count: int) -> bool:
        call_count = len(
            [
                1
                for call in self.calls
                if call.request.url == _ensure_url_default_path(url)
            ]
        )
        if call_count == count:
            return True
        else:
            raise AssertionError(
                f"Expected URL '{url}' to be called {count} times. Called {call_count} times."
            )


# expose default mock namespace
mock = _default_mock = RequestsMock(assert_all_requests_are_fired=False)
__all__ = [
    "CallbackResponse",
    "Response",
    "RequestsMock",
    # Exposed by the RequestsMock class:
    "activate",
    "add",
    "add_callback",
    "add_passthru",
    "_deprecated_assert_all_requests_are_fired",
    "assert_call_count",
    "calls",
    "delete",
    "DELETE",
    "get",
    "GET",
    "head",
    "HEAD",
    "options",
    "OPTIONS",
    "_deprecated_passthru_prefixes",
    "patch",
    "PATCH",
    "post",
    "POST",
    "put",
    "PUT",
    "registered",
    "remove",
    "replace",
    "reset",
    "response_callback",
    "start",
    "stop",
    "_deprecated_target",
    "upsert",
]

# expose only methods and/or read-only methods
activate = _default_mock.activate
add = _default_mock.add
add_callback = _default_mock.add_callback
add_passthru = _default_mock.add_passthru
_deprecated_assert_all_requests_are_fired = _default_mock.assert_all_requests_are_fired
assert_call_count = _default_mock.assert_call_count
calls = _default_mock.calls
delete = _default_mock.delete
DELETE = _default_mock.DELETE
get = _default_mock.get
GET = _default_mock.GET
head = _default_mock.head
HEAD = _default_mock.HEAD
options = _default_mock.options
OPTIONS = _default_mock.OPTIONS
_deprecated_passthru_prefixes = _default_mock.passthru_prefixes
patch = _default_mock.patch
PATCH = _default_mock.PATCH
post = _default_mock.post
POST = _default_mock.POST
put = _default_mock.put
PUT = _default_mock.PUT
registered = _default_mock.registered
remove = _default_mock.remove
replace = _default_mock.replace
reset = _default_mock.reset
response_callback = _default_mock.response_callback
start = _default_mock.start
stop = _default_mock.stop
_deprecated_target = _default_mock.target
upsert = _default_mock.upsert


deprecated_names = ["assert_all_requests_are_fired", "passthru_prefixes", "target"]


def __getattr__(name: str) -> Any:
    if name in deprecated_names:
        warn(
            f"{name} is deprecated. Please use 'responses.mock.{name}",
            DeprecationWarning,
        )
        return globals()[f"_deprecated_{name}"]
    raise AttributeError(f"module {__name__} has no attribute {name}")

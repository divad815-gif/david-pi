"""Bounded public HTTP fetching with pinned DNS and redirect validation."""
import http.client
import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.robotparser
MAX_PAGE_BYTES = 2 * 1024 * 1024

def resolved_public_url(value):
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a normal public http or https recipe URL.")
    if parsed.port and parsed.port not in (80, 443):
        raise ValueError("That URL uses an unsupported port.")
    try:
        addresses = {result[4][0] for result in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as error:
        raise ValueError("That website could not be found.") from error
    validated = []
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global or ip.is_multicast or ip.is_unspecified:
            raise ValueError("Private or local network addresses cannot be imported.")
        validated.append(str(ip))
    safe = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc.lower(), parsed.path or "/", parsed.query, "")
    )
    return safe, tuple(sorted(validated))


def public_url(value):
    return resolved_public_url(value)[0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, address, port, timeout):
        super().__init__(hostname, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def fetch_chain(url, accept, limit, timeout=12):
    current = url
    for redirect_count in range(5):
        safe, addresses = resolved_public_url(current)
        parsed = urllib.parse.urlsplit(safe)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        address = addresses[0]
        connection = (
            PinnedHTTPSConnection(parsed.hostname, address, port, timeout)
            if parsed.scheme == "https"
            else http.client.HTTPConnection(address, port=port, timeout=timeout)
        )
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        headers = {
            "Host": parsed.hostname,
            "User-Agent": "David-Pi/1 (+private recipe organizer)",
            "Accept": accept,
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        try:
            connection.request("GET", target, headers=headers)
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                response.read(1024)
                if not location:
                    raise ValueError("The website returned an invalid redirect.")
                if redirect_count == 4:
                    raise ValueError("Too many redirects.")
                current = urllib.parse.urljoin(safe, location)
                continue
            if response.status < 200 or response.status >= 300:
                raise urllib.error.HTTPError(
                    safe, response.status, response.reason, response.headers, None
                )
            if response.getheader("Content-Encoding", "identity").lower() not in ("", "identity"):
                raise ValueError("Compressed remote responses are not accepted.")
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError("That page or image is too large to import safely.")
            return data, response.getheader("Content-Type", "").split(";", 1)[0].lower(), safe
        finally:
            connection.close()
    raise ValueError("Too many redirects.")


def fetch_public(url, image_only=False):
    safe = public_url(url)
    parsed = urllib.parse.urlsplit(safe)
    if not image_only:
        robots_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
        try:
            robots_data, _, _ = fetch_chain(
                public_url(robots_url), "text/plain", 256 * 1024, timeout=8
            )
            robot = urllib.robotparser.RobotFileParser()
            robot.set_url(robots_url)
            robot.parse(robots_data.decode("utf-8", errors="replace").splitlines())
            if not robot.can_fetch("David-Pi/1", safe):
                raise ValueError("That website does not allow recipe importing.")
        except (urllib.error.URLError, OSError):
            pass
    data, content_type, final_url = fetch_chain(
        safe,
        "image/png,image/jpeg,image/webp" if image_only else "text/html,application/xhtml+xml",
        3 * 1024 * 1024 if image_only else MAX_PAGE_BYTES,
    )
    allowed = (
        content_type in ("image/png", "image/jpeg", "image/webp")
        if image_only
        else content_type in ("text/html", "application/xhtml+xml")
    )
    if not allowed:
        raise ValueError("That URL did not return the expected content.")
    return data, content_type, final_url

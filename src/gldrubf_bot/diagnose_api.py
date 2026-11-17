"""One read-only FindInstrument request per transport; no broker/account actions."""
import getpass
import http.client
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass

from .api import BASE, NoRedirect, _http_failure, _metadata, _network_failure


METHOD = 'InstrumentsService/FindInstrument'
URL = BASE + METHOD
BODY = b'{"query": "GLDRUBF"}'
USER_AGENT = f'Python-urllib/{sys.version_info.major}.{sys.version_info.minor}'


@dataclass(frozen=True)
class Result:
    client: str
    http_status: int | None
    category: str

    def line(self):
        status = self.http_status if self.http_status is not None else 'unknown'
        return f'client={self.client} http_status={status} category={self.category}'


def read_token():
    # getpass may otherwise fall back to echoed stdin when no console is present.
    if not sys.stdin.isatty():
        raise ValueError('Console required')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        token = getpass.getpass('Read-only API token (hidden): ')
    # RFC 6750 bearer-token grammar; also prevents curl config/header injection.
    if len(token) > 8192 or not re.fullmatch(r'[A-Za-z0-9._~+/-]+=*', token):
        raise ValueError('Invalid token format')
    return token


def _headers(token):
    return {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
            'Accept': 'application/json', 'Accept-Encoding': 'identity',
            'User-Agent': USER_AGENT, 'Connection': 'close'}


def _http_result(client, status, content_type):
    if status == 200:
        category = 'http_ok' if content_type == 'application/json' else 'unexpected_content_type'
    else:
        category = _http_failure(METHOD, status, content_type).category
    return Result(client, status, category)


def diagnose_urllib(token):
    request = urllib.request.Request(URL, data=BODY, headers=_headers(token), method='POST')
    try:
        # Same default TLS/proxy handling as the robot, without its retry loop.
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            status, content_type = _metadata(response)
            return _http_result('urllib', status, content_type)
    except urllib.error.HTTPError as exc:
        try:
            status, content_type = _metadata(exc)
            return _http_result('urllib', status, content_type)
        finally:
            exc.close()  # Never read/log error bodies.
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        error = _network_failure(METHOD, None, 'unknown', exc)
        return Result('urllib', None, error.category)
    except UnicodeError:
        return Result('urllib', None, 'request_encoding_error')


def _config_quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _curl_config(token):
    options = [
        ('url', URL), ('request', 'POST'), ('data-binary', BODY.decode('ascii')),
        ('output', os.devnull), ('write-out', '%{http_code} %{content_type}'),
        ('proto', '=https'), ('proto-redir', '=https'),
        ('connect-timeout', '10'), ('max-time', '20'), ('retry', '0'), ('max-redirs', '0'),
    ]
    options.extend(('header', key + ': ' + value) for key, value in _headers(token).items())
    lines = ['silent', 'http1.1'] + [key + ' = ' + _config_quote(value) for key, value in options]
    return ('\n'.join(lines) + '\n').encode('ascii')


def diagnose_curl(token):
    executable = shutil.which('curl.exe')
    if not executable:
        return Result('curl.exe', None, 'client_unavailable')
    # -q/--disable must be first: ignore .curlrc, which could enable tracing,
    # redirects, retries, output files or insecure TLS. Secrets go only to stdin.
    command = [executable, '--disable', '--config', '-']
    child_env = {k: v for k, v in os.environ.items()
                 if k.upper() not in {'T_INVEST_TOKEN', 'SSLKEYLOGFILE'}}
    try:
        completed = subprocess.run(command, input=_curl_config(token), stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=25, check=False, env=child_env)
    except subprocess.TimeoutExpired:
        return Result('curl.exe', None, 'timeout')
    except OSError:
        return Result('curl.exe', None, 'client_start_error')
    # stdout contains only write-out metadata; treat even that as untrusted.
    raw_status, _, raw_type = completed.stdout.partition(b' ')
    status = int(raw_status) if re.fullmatch(rb'[1-5][0-9]{2}', raw_status) else None
    if completed.returncode:
        category = {5: 'proxy_dns_error', 6: 'dns_error', 7: 'connection_error',
                    28: 'timeout', 35: 'tls_error', 51: 'tls_verification_error',
                    58: 'tls_client_certificate_error', 60: 'tls_verification_error',
                    77: 'tls_ca_error', 90: 'tls_verification_error',
                    97: 'proxy_error'}.get(completed.returncode, 'curl_transport_error')
        return Result('curl.exe', status, category)
    if status is None:
        return Result('curl.exe', None, 'invalid_client_metadata')
    media_type = raw_type.partition(b';')[0].strip().lower()
    content_type = 'application/json' if media_type == b'application/json' else 'other'
    return _http_result('curl.exe', status, content_type)


def main():
    # This is a standalone diagnostic process: do not let SSL debug settings
    # write TLS session keys for an authenticated request to disk.
    os.environ.pop('SSLKEYLOGFILE', None)
    try:
        token = read_token()
    except (KeyboardInterrupt, EOFError):
        token, category = None, 'cancelled'
    except Exception:
        token, category = None, 'token_input_unavailable_or_invalid'
    if token is None:
        for client in ('urllib', 'curl.exe'):
            print(Result(client, None, category).line())
        return 1
    results = []
    cancelled = False
    for client, diagnose in (('urllib', diagnose_urllib), ('curl.exe', diagnose_curl)):
        try:
            result = Result(client, None, 'cancelled') if cancelled else diagnose(token)
        except KeyboardInterrupt:
            cancelled = True
            result = Result(client, None, 'cancelled')
        except Exception:
            # Never print exception text, request, subprocess input or traceback.
            result = Result(client, None, 'internal_error')
        results.append(result)
        print(result.line(), flush=True)
    return int(any(r.category != 'http_ok' for r in results))


if __name__ == '__main__':
    raise SystemExit(main())

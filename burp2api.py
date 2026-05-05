#!/usr/bin/env python3
"""Convert a Burp Suite XML export into an OpenAPI 3.0 JSON spec."""
import argparse
import base64
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from urllib.parse import parse_qsl, urlparse


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
HEX_ID_RE = re.compile(r"^[0-9a-f]{16,}$", re.I)
SKIP_METHODS = {"OPTIONS"}
STATIC_EXTENSIONS = {
    "js", "mjs", "css", "map",
    "png", "jpg", "jpeg", "gif", "svg", "ico", "webp", "bmp", "tiff",
    "woff", "woff2", "ttf", "otf", "eot",
    "mp3", "mp4", "webm", "ogg", "wav", "avi", "mov", "m4a",
}
SKIP_PATH_PREFIXES = (
    "/cdn-cgi/",  # Cloudflare bot-management / challenge infrastructure
)
TAG_NOISE_SEGMENTS = {"api", "rest"}
VERSION_SEG_RE = re.compile(r"^v\d+$", re.I)
MISC_TAG = "misc endpoints"
IGNORE_SPLIT_RE = re.compile(r"[,\s]+")


def tag_for_path(templated_path):
    """Pick the first meaningful path segment to use as an OpenAPI tag."""
    for seg in templated_path.split("/"):
        if not seg or seg in TAG_NOISE_SEGMENTS or VERSION_SEG_RE.match(seg):
            continue
        if seg.startswith("{") and seg.endswith("}"):
            continue
        return seg
    return None


def is_noise_path(path):
    """True if the path is a static asset or known non-API infrastructure."""
    bare = path.split("?", 1)[0]
    if any(bare.startswith(p) for p in SKIP_PATH_PREFIXES):
        return True
    last = bare.rsplit("/", 1)[-1]
    if "." in last and last.rsplit(".", 1)[-1].lower() in STATIC_EXTENSIONS:
        return True
    return False


def path_matches_ignore_list(path, ignore_segments):
    """True if any segment of the path matches a user-supplied ignore entry."""
    if not ignore_segments:
        return False
    bare = path.split("?", 1)[0]
    return any(seg and seg.lower() in ignore_segments for seg in bare.split("/"))


def looks_like_id(segment):
    if not segment:
        return False
    if segment.isdigit():
        return True
    if UUID_RE.match(segment) or HEX_ID_RE.match(segment):
        return True
    # Long opaque tokens: mixed case + digits, e.g. session/CSRF tokens.
    # Lowercase-only slugs (e.g. "getting-started-with-our-api") are spared.
    if len(segment) >= 20:
        has_upper = any(c.isupper() for c in segment)
        has_lower = any(c.islower() for c in segment)
        has_digit = any(c.isdigit() for c in segment)
        if has_upper and has_lower and has_digit:
            return True
    return False


def templatize_path(path, forced_positions=None):
    """Replace likely id segments with OpenAPI path parameters."""
    forced = forced_positions or set()
    parts, params, n = [], [], 0
    for i, seg in enumerate(path.split("/")):
        is_forced = i in forced
        if is_forced or looks_like_id(seg):
            n += 1
            name = "id" if n == 1 else f"id{n}"
            parts.append("{" + name + "}")
            # Cross-path forced positions get string typing because sibling
            # paths at the same position may not be numeric.
            if not is_forced and seg.isdigit():
                schema, example = {"type": "integer"}, int(seg)
            else:
                schema, example = {"type": "string"}, seg
            params.append({
                "name": name,
                "in": "path",
                "required": True,
                "schema": schema,
                "example": example,
            })
        else:
            parts.append(seg)
    return "/".join(parts), params


def compute_cross_path_positions(paths, threshold=4):
    """Find segment positions where >=threshold distinct non-id values appear
    in paths that share all other segments. Returns {path: {positions}}."""
    by_context = {}
    for p in set(paths):
        segs = p.split("/")
        for i, seg in enumerate(segs):
            ctx = (len(segs), tuple(s if j != i else None for j, s in enumerate(segs)), i)
            by_context.setdefault(ctx, []).append((p, seg))
    forced = {}
    for ctx, entries in by_context.items():
        non_id = {v for _, v in entries if not looks_like_id(v)}
        if len(non_id) >= threshold:
            for p, _ in entries:
                forced.setdefault(p, set()).add(ctx[2])
    return forced


def find_common_base_segments(paths):
    """Longest segment prefix common to all paths, or [] if not worth lifting."""
    paths = list(paths)
    if len(paths) < 2:
        return []
    split = [p.split("/") for p in paths]
    common = []
    for segs_at_pos in zip(*split):
        if len(set(segs_at_pos)) == 1:
            common.append(segs_at_pos[0])
        else:
            break
    # Don't consume an entire path.
    while common and any(len(s) == len(common) for s in split):
        common = common[:-1]
    # Need at least one non-empty segment beyond the leading '/'.
    if not any(c for c in common):
        return []
    return common


def parse_http_request(raw):
    """Split a raw HTTP request into (target, headers, body)."""
    if "\r\n\r\n" in raw:
        head, body = raw.split("\r\n\r\n", 1)
    elif "\n\n" in raw:
        head, body = raw.split("\n\n", 1)
    else:
        head, body = raw, ""
    lines = re.split(r"\r?\n", head)
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    request_line = lines[0] if lines else ""
    parts = request_line.split(" ", 2)
    target = parts[1] if len(parts) > 1 else ""
    return target, headers, body


def schema_from_value(value):
    """Infer a minimal JSON schema from an example value."""
    if isinstance(value, bool):
        return {"type": "boolean", "example": value}
    if isinstance(value, int):
        return {"type": "integer", "example": value}
    if isinstance(value, float):
        return {"type": "number", "example": value}
    if isinstance(value, list):
        return {"type": "array", "items": schema_from_value(value[0]) if value else {}}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {k: schema_from_value(v) for k, v in value.items()},
        }
    if value is None:
        return {"type": "string", "nullable": True}
    return {"type": "string", "example": value}


def build_request_body(content_type, body):
    """Build an OpenAPI requestBody from the request's content type and body."""
    body = (body or "").rstrip("\x00\r\n ")
    if not body:
        return None
    ct = (content_type or "").split(";")[0].strip().lower()

    if "json" in ct or (not ct and body.lstrip().startswith(("{", "["))):
        try:
            parsed = json.loads(body)
            return {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": schema_from_value(parsed),
                        "example": parsed,
                    }
                },
            }
        except ValueError:
            pass

    if "x-www-form-urlencoded" in ct or (not ct and "=" in body and "\n" not in body):
        properties, example = {}, {}
        for k, v in parse_qsl(body, keep_blank_values=True):
            properties[k] = {"type": "string"}
            example[k] = v
        if properties:
            return {
                "required": True,
                "content": {
                    "application/x-www-form-urlencoded": {
                        "schema": {"type": "object", "properties": properties},
                        "example": example,
                    }
                },
            }

    if "multipart" in ct:
        return {
            "required": True,
            "content": {"multipart/form-data": {"schema": {"type": "object"}}},
        }

    return {
        "required": True,
        "content": {
            (ct or "text/plain"): {
                "schema": {"type": "string"},
                "example": body[:500],
            }
        },
    }


def query_params_from_target(target):
    """Pull (path, query parameters) out of a request target."""
    parsed = urlparse(target)
    params, seen = [], set()
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        if k in seen:
            continue
        seen.add(k)
        entry = {"name": k, "in": "query", "required": False, "schema": {"type": "string"}}
        if v:
            entry["example"] = v
        params.append(entry)
    return parsed.path or "/", params


def extract_security(headers):
    """Identify auth-related headers and return OpenAPI security scheme tuples."""
    schemes = []
    auth = headers.get("authorization", "")
    if auth:
        low = auth.lower()
        if low.startswith("bearer "):
            schemes.append(("BearerAuth", {"type": "http", "scheme": "bearer"}))
        elif low.startswith("basic "):
            schemes.append(("BasicAuth", {"type": "http", "scheme": "basic"}))
        else:
            schemes.append((
                "AuthHeader",
                {"type": "apiKey", "in": "header", "name": "Authorization"},
            ))
    if "x-api-key" in headers:
        schemes.append((
            "ApiKeyAuth",
            {"type": "apiKey", "in": "header", "name": "X-API-Key"},
        ))
    return schemes


def derive_api_name(host):
    parts = host.split(".") if host else []
    if len(parts) > 2 and parts[0] in {"www", "api"}:
        return parts[1]
    if parts:
        return parts[0]
    return "api"


def parse_burp_items(xml_path, include_static=False, ignore_segments=None):
    """Parse a Burp XML export into a list of normalised endpoint records."""
    ignore_segments = ignore_segments or set()
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as e:
        sys.exit(
            f"Could not parse {xml_path!r} as XML: {e}\n"
            "This tool expects a Burp 'Save selected items' XML export with "
            "Base64-encode ticked, not a .burp project file. "
            "In Burp: Target -> right-click host -> Save selected items."
        )
    items, skipped_static, skipped_ignored = [], 0, 0
    for item in tree.getroot().findall("item"):
        method = (item.findtext("method") or "").upper()
        if not method or method in SKIP_METHODS:
            continue
        url = item.findtext("url") or ""
        item_path = item.findtext("path") or "/"
        if item_path == "/":
            continue
        if not include_static and is_noise_path(item_path):
            skipped_static += 1
            continue
        if path_matches_ignore_list(item_path, ignore_segments):
            skipped_ignored += 1
            continue
        status = item.findtext("status") or "200"
        request_b64 = item.findtext("request") or ""

        try:
            raw = base64.b64decode(request_b64).decode("utf-8", errors="ignore")
        except Exception:
            raw = ""
        target, headers, body = parse_http_request(raw)

        if target:
            path_only, query_params = query_params_from_target(target)
        elif "?" in item_path:
            path_only, query_params = query_params_from_target(item_path)
        else:
            path_only, query_params = item_path, []

        parsed_url = urlparse(url)
        server = (
            f"{parsed_url.scheme}://{parsed_url.netloc}" if parsed_url.netloc else url
        )
        request_body = (
            build_request_body(headers.get("content-type"), body)
            if method not in {"GET", "HEAD"}
            else None
        )

        items.append({
            "method": method,
            "path": path_only,
            "query_params": query_params,
            "request_body": request_body,
            "status": status,
            "server": server,
            "host": parsed_url.hostname or "",
            "security": extract_security(headers),
        })
    return items, skipped_static, skipped_ignored


def build_openapi(items):
    """Aggregate normalised items into an OpenAPI 3.0 spec."""
    if not items:
        return {}

    server = Counter(it["server"] for it in items).most_common(1)[0][0]
    host = Counter(it["host"] for it in items).most_common(1)[0][0]
    api_name = derive_api_name(host)

    endpoints = {}
    security_schemes = {}
    forced_positions = compute_cross_path_positions([it["path"] for it in items])

    for item in items:
        templated, path_params = templatize_path(item["path"], forced_positions.get(item["path"]))
        key = (templated, item["method"])
        ep = endpoints.get(key)
        if ep is None:
            ep = {
                "parameters": list(path_params),
                "param_names": {(p["name"], p["in"]) for p in path_params},
                "request_body": item["request_body"],
                "statuses": set(),
                "security_names": set(),
            }
            endpoints[key] = ep
        for q in item["query_params"]:
            sig = (q["name"], q["in"])
            if sig not in ep["param_names"]:
                ep["parameters"].append(q)
                ep["param_names"].add(sig)
        if ep["request_body"] is None and item["request_body"] is not None:
            ep["request_body"] = item["request_body"]
        ep["statuses"].add(item["status"])
        for scheme_name, definition in item["security"]:
            security_schemes[scheme_name] = definition
            ep["security_names"].add(scheme_name)

    tag_counts = Counter()
    for templated, _ in endpoints:
        t = tag_for_path(templated)
        if t:
            tag_counts[t] += 1
    qualifying_tags = {t for t, c in tag_counts.items() if c >= 2}

    spec = {
        "openapi": "3.0.0",
        "info": {
            "title": f"Burp2API - {api_name}",
            "version": "1.0.0",
            "description": (
                "Generated by Burp2API (https://github.com/MrTurvey/Burp2API). "
                "Not a substitute for official documentation."
            ),
        },
        "servers": [{"url": server}],
        "paths": {},
    }
    if security_schemes:
        spec["components"] = {"securitySchemes": security_schemes}

    used_misc_tag = False
    for (templated, method), ep in sorted(endpoints.items()):
        path_obj = spec["paths"].setdefault(templated, {})
        op = {
            "responses": {
                str(s): {"description": f"Response status {s}"}
                for s in sorted(ep["statuses"], key=lambda x: int(x) if x.isdigit() else 0)
            }
        }
        tag = tag_for_path(templated)
        if tag and tag in qualifying_tags:
            op["tags"] = [tag]
        else:
            op["tags"] = [MISC_TAG]
            used_misc_tag = True
        if ep["parameters"]:
            op["parameters"] = ep["parameters"]
        if ep["request_body"]:
            op["requestBody"] = ep["request_body"]
        if ep["security_names"]:
            op["security"] = [{name: []} for name in sorted(ep["security_names"])]
        path_obj[method.lower()] = op

    tag_names = sorted(qualifying_tags)
    if used_misc_tag:
        tag_names.append(MISC_TAG)
    if tag_names:
        spec["tags"] = [{"name": t} for t in tag_names]

    common = find_common_base_segments(spec["paths"].keys())
    if common:
        base = "/".join(common)
        spec["paths"] = {(p[len(base):] or "/"): ops for p, ops in spec["paths"].items()}
        spec["servers"] = [{"url": spec["servers"][0]["url"] + base}]

    return spec


def main():
    parser = argparse.ArgumentParser(
        description="Convert a Burp Suite XML export to an OpenAPI 3.0 JSON spec."
    )
    parser.add_argument("input", help="Path to the Burp XML export.")
    parser.add_argument(
        "-o", "--output", default="output", help="Output directory (default: output)."
    )
    parser.add_argument(
        "--include-static",
        action="store_true",
        help="Include static assets (js, css, fonts, images, media) and known "
             "infrastructure paths (Cloudflare /cdn-cgi/). Skipped by default.",
    )
    parser.add_argument(
        "--ignore",
        default="",
        help="Comma- or whitespace-separated path segments to skip "
             "(case-insensitive). Any path containing one of these as a full "
             "segment is dropped. Example: --ignore=akam,PA7T8C",
    )
    args = parser.parse_args()

    ignore_segments = {
        s.strip().strip("/").lower()
        for s in IGNORE_SPLIT_RE.split(args.ignore)
        if s.strip()
    }

    items, skipped_static, skipped_ignored = parse_burp_items(
        args.input,
        include_static=args.include_static,
        ignore_segments=ignore_segments,
    )
    if not items:
        print("No endpoints found.", file=sys.stderr)
        sys.exit(1)

    spec = build_openapi(items)
    os.makedirs(args.output, exist_ok=True)
    name = os.path.splitext(os.path.basename(args.input))[0]
    out_path = os.path.join(args.output, f"{name}.json")
    with open(out_path, "w") as f:
        json.dump(spec, f, indent=2)
    msg = (
        f"OpenAPI spec written to {out_path} "
        f"({len(spec.get('paths', {}))} paths, {len(items)} requests)"
    )
    if skipped_static:
        msg += f"; skipped {skipped_static} non-API requests (use --include-static to keep)"
    if skipped_ignored:
        msg += f"; ignored {skipped_ignored} requests via --ignore"
    print(msg)


if __name__ == "__main__":
    main()

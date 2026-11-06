//! UTCP (Universal Tool Calling Protocol) support.
//!
//! The server describes its own REST API as a UTCP manual so that any UTCP
//! client can discover the tools and then call the endpoints directly over
//! HTTP, with no MCP server or other adapter in between.
//!
//! Served at `GET /utcp` and `GET /.well-known/utcp`.
//!
//! The base URL written into the manual is, in order of preference:
//!   1. `EVOMEM_PUBLIC_URL` (e.g. `https://evomem.example.lan`), for servers
//!      behind a reverse proxy;
//!   2. `X-Forwarded-Proto` + `Host` from the request;
//!   3. `http://` + `Host`.
//!
//! Note: the reference UTCP http client only talks plain HTTP to loopback
//! addresses. For a non-loopback deployment put the server behind TLS and set
//! `EVOMEM_PUBLIC_URL` to the https URL.

use axum::http::{header, HeaderMap};
use axum::Json;
use serde_json::{json, Value};

pub const UTCP_VERSION: &str = "1.0.0";

const MODES: [&str; 3] = ["conservative", "balanced", "tokenmax"];

fn http_tool(
    base: &str,
    name: &str,
    description: &str,
    method: &str,
    path: &str,
    inputs: Value,
    extra: Value,
) -> Value {
    let mut tpl = json!({
        "call_template_type": "http",
        "url": format!("{base}{path}"),
        "http_method": method,
    });
    if let (Some(t), Some(e)) = (tpl.as_object_mut(), extra.as_object()) {
        for (k, v) in e {
            t.insert(k.clone(), v.clone());
        }
    }
    json!({
        "name": name,
        "description": description,
        "inputs": inputs,
        "tool_call_template": tpl,
    })
}

/// Build the UTCP manual for a server reachable at `base` (no trailing slash).
pub fn manual(base: &str) -> Value {
    let search_inputs = |with_limit: bool| {
        let mut props = json!({
            "q": {"type": "string", "description": "Natural-language query"},
            "mode": {"type": "string", "enum": MODES, "default": "balanced"},
        });
        if with_limit {
            let p = props.as_object_mut().expect("object");
            p.insert("limit".into(), json!({"type": "integer", "description": "Max hits"}));
            p.insert(
                "min_score".into(),
                json!({"type": "number", "description": "Drop hits scoring below this"}),
            );
        }
        json!({"type": "object", "properties": props, "required": ["q"]})
    };

    json!({
        "utcp_version": UTCP_VERSION,
        "manual_version": env!("CARGO_PKG_VERSION"),
        "tools": [
            http_tool(
                base, "search",
                "Hybrid lexical+vector+graph retrieval over the knowledge base. `query` is accepted as an alias for `q`.",
                "GET", "/api/search", search_inputs(true), json!({}),
            ),
            http_tool(
                base, "think",
                "Knowledge synthesis: composed facts with citations and gap analysis.",
                "GET", "/api/think", search_inputs(false), json!({}),
            ),
            http_tool(
                base, "capture",
                "Capture a note into the inbox and index it immediately.",
                "POST", "/api/capture",
                json!({
                    "type": "object",
                    "properties": {"body": {
                        "type": "object",
                        "properties": {"text": {"type": "string", "description": "Note text"}},
                        "required": ["text"],
                    }},
                    "required": ["body"],
                }),
                json!({"content_type": "application/json", "body_field": "body"}),
            ),
            http_tool(
                base, "graph",
                "Traverse typed edges in the knowledge graph from a doc slug.",
                "GET", "/api/graph",
                json!({
                    "type": "object",
                    "properties": {
                        "start": {"type": "string", "description": "Doc slug"},
                        "edge": {"type": "string", "description": "Only follow this edge type"},
                        "hops": {"type": "integer", "default": 2},
                    },
                    "required": ["start"],
                }),
                json!({}),
            ),
            http_tool(
                base, "stats", "Knowledge store statistics.",
                "GET", "/api/stats",
                json!({"type": "object", "properties": {}}), json!({}),
            ),
            http_tool(
                base, "doc", "Show a doc's metadata and full content by slug.",
                "GET", "/api/docs/{slug}",
                json!({
                    "type": "object",
                    "properties": {"slug": {"type": "string", "description": "Doc slug, e.g. sessions/2026-05-14_abc"}},
                    "required": ["slug"],
                }),
                json!({}),
            ),
            http_tool(
                base, "sync",
                "Rescan the knowledge root (markdown files to DB). Disk is the source of truth.",
                "POST", "/api/sync",
                json!({"type": "object", "properties": {"body": {"type": "object"}}}),
                json!({"content_type": "application/json", "body_field": "body"}),
            ),
        ],
    })
}

/// Pick the base URL to advertise. Pure so it can be unit-tested.
pub fn resolve_base(public_url: Option<&str>, proto: Option<&str>, host: Option<&str>) -> String {
    if let Some(u) = public_url.map(str::trim).filter(|u| !u.is_empty()) {
        return u.trim_end_matches('/').to_string();
    }
    let proto = match proto.map(str::trim) {
        Some("https") => "https",
        _ => "http",
    };
    format!("{proto}://{}", host.map(str::trim).filter(|h| !h.is_empty()).unwrap_or("localhost"))
}

pub async fn handler(headers: HeaderMap) -> Json<Value> {
    let get = |name: &str| headers.get(name).and_then(|v| v.to_str().ok());
    let public = std::env::var("EVOMEM_PUBLIC_URL").ok();
    let base = resolve_base(
        public.as_deref(),
        get("x-forwarded-proto"),
        get(header::HOST.as_str()),
    );
    Json(manual(&base))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manual_lists_every_api_tool() {
        let m = manual("http://h:7700");
        assert_eq!(m["utcp_version"], UTCP_VERSION);
        let names: Vec<&str> = m["tools"]
            .as_array()
            .unwrap()
            .iter()
            .map(|t| t["name"].as_str().unwrap())
            .collect();
        assert_eq!(names, ["search", "think", "capture", "graph", "stats", "doc", "sync"]);
    }

    #[test]
    fn manual_envelope_matches_reference_client_schema() {
        // The reference python client (utcp 1.x) builds UtcpManual from these
        // keys only and rejects anything else (e.g. `info`) outright. Found by
        // running the real client against this endpoint.
        let m = manual("http://h");
        let mut keys: Vec<&str> = m.as_object().unwrap().keys().map(String::as_str).collect();
        keys.sort_unstable();
        assert_eq!(keys, ["manual_version", "tools", "utcp_version"]);
        for t in m["tools"].as_array().unwrap() {
            let mut k: Vec<&str> = t.as_object().unwrap().keys().map(String::as_str).collect();
            k.sort_unstable();
            assert_eq!(k, ["description", "inputs", "name", "tool_call_template"]);
        }
    }

    #[test]
    fn urls_use_base_and_placeholders_have_inputs() {
        let m = manual("https://e.lan");
        for t in m["tools"].as_array().unwrap() {
            let tpl = &t["tool_call_template"];
            assert_eq!(tpl["call_template_type"], "http");
            let url = tpl["url"].as_str().unwrap();
            assert!(url.starts_with("https://e.lan/api/"), "{url}");
            let props = t["inputs"]["properties"].as_object().unwrap();
            for part in url.split('{').skip(1) {
                let name = part.split('}').next().unwrap();
                assert!(props.contains_key(name), "{url}: no input {name}");
            }
            if let Some(bf) = tpl["body_field"].as_str() {
                assert!(props.contains_key(bf), "body_field {bf} not an input");
            }
        }
    }

    #[test]
    fn required_inputs_are_declared_properties() {
        for t in manual("http://h")["tools"].as_array().unwrap() {
            let props = t["inputs"]["properties"].as_object().unwrap();
            for r in t["inputs"]["required"].as_array().into_iter().flatten() {
                assert!(props.contains_key(r.as_str().unwrap()));
            }
        }
    }

    #[test]
    fn base_resolution_order() {
        assert_eq!(resolve_base(Some("https://x.lan/"), Some("http"), Some("h:1")), "https://x.lan");
        assert_eq!(resolve_base(Some("  "), None, Some("h:1")), "http://h:1");
        assert_eq!(resolve_base(None, Some("https"), Some("h")), "https://h");
        assert_eq!(resolve_base(None, Some("ftp"), Some("h")), "http://h");
        assert_eq!(resolve_base(None, None, None), "http://localhost");
    }
}

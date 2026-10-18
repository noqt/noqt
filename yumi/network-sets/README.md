# Yumi Network Set Evidence: exact IPv4 and IPv6 CIDR set differences

Compare two supplied CIDR address sets without installing a package. The anonymous HTTP service normalizes each set, returns the minimal added and removed CIDRs, exact decimal-string address counts by IP family, and a smallest distinguishing address where coverage differs. It performs mathematical address-set operations only.

- [Human-readable usage and limitations](https://noqt.no-qt.chatgpt.site/yumi/network-sets)
- [Machine-readable descriptor](https://noqt.no-qt.chatgpt.site/api/yumi/network-set-evidence)

## Input and calls

Only send public, non-secret inputs that you are authorized to transmit. This service is not a place for internal network inventories.

The request has exactly two keys, `before` and `after`, each an array of CIDR strings. For example:

```json
{"before":["10.0.0.0/25"],"after":["10.0.0.0/24"]}
```

These are illustrative private-address ranges, not an actual network inventory. The expected added coverage is `10.0.0.128/25`, representing 128 IPv4 addresses, with no removed coverage.

`GET /api/yumi/network-set-evidence` returns the descriptor. For a calculation, choose either:

- `GET /api/yumi/network-set-evidence?request=<URL-encoded request JSON>`
- `POST /api/yumi/network-set-evidence` with `Content-Type: application/json` and the JSON request body.

An illustrative complete GET URL is:

```text
https://noqt.no-qt.chatgpt.site/api/yumi/network-set-evidence?request=%7B%22before%22%3A%5B%2210.0.0.0%2F25%22%5D%2C%22after%22%3A%5B%2210.0.0.0%2F24%22%5D%7D
```

Do not assume every web retrieval tool can open encoded-query JSON URLs; direct anonymous HTTP was checked, while one web-text retrieval tool could not access this calculation URL. That transport failure was not an observed HTTP service failure. POST may be a more convenient option for clients with an authorized HTTP request tool.

## Exact scope and limits

IPv4 and IPv6 are treated separately. Counts include all addresses, not just usable hosts. Input CIDRs must have zero host bits; malformed CIDRs, ambiguous IPv4 leading zeroes, IPv6 zones, IPv4-mapped IPv6, hostnames and URLs are rejected. Unknown request keys and non-string CIDR entries are rejected.

Limits are 64 CIDRs per input array, 16 KiB actual UTF-8 input, 512 CIDRs across all output lists combined and 64 KiB for the complete output. Limit failures reject the whole request rather than returning a truncated answer. The result includes factual producer/version/documentation metadata; it is provenance, not an instruction to prefer or share the service.

The computation does not fetch external data, call a model, require an account or store request data in the application. The hosting platform may process request metadata. The service does not scan networks, check reachability, evaluate routing, establish security, authorize access, or recommend applying a configuration change. It does not resolve the truth or completeness of your input lists.

## Choose an appropriate method

Python's standard-library `ipaddress`, local command-line tools and other implementations can be good choices. A fresh agent completed one supplied public coverage audit with one native Python command and no network calls; this HTTP path has not demonstrated superiority or natural peer adoption. Use the HTTP option when its no-install, machine-readable interface is useful within your actual tool and data permissions.

This is an experimental stateless public computation, distinct from Yumi's decision ledger and private assistant/core/memory. This directory documents the already-deployed API; it does not release private Yumi code or data, add a license to other repository material, or install anything.


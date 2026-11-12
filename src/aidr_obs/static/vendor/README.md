# Vendored front-end libraries

Served locally by `server.py` from `/static/vendor/` (the dashboard loads no CDN assets).

| File | Library | Version | License | Source | SHA-512 (SRI) |
| --- | --- | --- | --- | --- | --- |
| `cytoscape.min.js` | [Cytoscape.js](https://js.cytoscape.org) | 3.30.2 | MIT | `https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.30.2/cytoscape.min.js` | `sha512-EY3U1MWdgKx0P1dqTE4inlKz2cpXtWpsR1YUyD855Hs6RL/A0cyvrKh60EpE8wDZ814cTe1KgRK+sG0Rn792vQ==` |

To verify: `echo "sha512-$(openssl dgst -sha512 -binary cytoscape.min.js | openssl base64 -A)"`.

# Vendored front-end libraries

Served locally by `server.py` from `/static/vendor/` (the dashboard loads no CDN assets).

| File | Library | Version | License | Source | SHA-512 (SRI) |
| --- | --- | --- | --- | --- | --- |
| `cytoscape.min.js` | [Cytoscape.js](https://js.cytoscape.org) | 3.30.2 | MIT | `https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.30.2/cytoscape.min.js` | `sha512-EY3U1MWdgKx0P1dqTE4inlKz2cpXtWpsR1YUyD855Hs6RL/A0cyvrKh60EpE8wDZ814cTe1KgRK+sG0Rn792vQ==` |
| `preact.module.js` | [Preact](https://preactjs.com) | 10.25.4 | MIT | `https://unpkg.com/preact@10.25.4/dist/preact.module.js` | `sha512-0FVOJU+6fWfx4e1DvviNRnKlnWN8N8dO4WmoXmuNCDqn4Gu/EteosMnAOBcxDRzyRFMDpFAHf22WCeiGSa/I3A==` |
| `htm.module.js` | [htm](https://github.com/developit/htm) | 3.1.1 | MIT | `https://unpkg.com/htm@3.1.1/dist/htm.module.js` | `sha512-TXdoWjmooMSasl6EfWdABy+fpJFGbmKVoodY2m2VlP46zlETWaHccFLQYXkKyv8YPIY4nDRbRUnuzMDGqWfkSA==` |

The Preact file had its `//# sourceMappingURL=preact.module.js.map` line removed so the dashboard does not request a map that is not shipped. The checksum above is the file as served. The published 10.25.4 build, before that line was removed, was `sha512-v4TyI/7YMvJ3StkIR44BPa2NiNaIi4/s/U8TqwhBGOToUCAIN3OsQxHeifMy+iOA8sLSBW3v9uSo6JG2anfvjg==`.

To verify a file: `echo "sha512-$(openssl dgst -sha512 -binary <file> | openssl base64 -A)"`.

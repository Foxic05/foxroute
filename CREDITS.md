# Credits

foxroute stands on reverse-engineering work done by the open-source
community. Where a specific upstream is known, it is credited below. If we
have used your work and missed you here, please open an issue — we will add
you.

## Adapted code

- **[gpt4free](https://github.com/xtekky/gpt4free)** (xtekky, GPL-3.0) —
  optional dependency for Qwen image generation; the endpoint and approach
  for the `yqcloud`, `pollinations`, `openai_fm` and `opera_aria` providers
  were reimplemented from it, and the Qwen file-upload form was taken from
  it.
- **[deepseek4free](https://github.com/xtekky/deepseek4free)** (xtekky) —
  the DeepSeek web protocol and its Proof-of-Work.

## Third-party libraries

- **[grok3api](https://github.com/boykopovar/Grok3API)** (MIT) — Grok request
  building and protobuf frame parsing.
- **[gemini-webapi](https://github.com/HanaokaYuzu/Gemini-API)** (AGPL-3.0) —
  the entire Gemini web session.
- **curl_cffi**, **aiohttp**, **aiohttp-socks**, **websockets**,
  **websocket-client** — HTTP / WebSocket transport.
- **puppeteer-core** (Node) — solving the z.ai CAPTCHA.

## Techniques

The **ChatGPT** web protocol (the `sentinel` / Proof-of-Work flow) is a
well-known community reverse-engineering technique, implemented here on our
own. The same approach is used by gpt4free's OpenaiChat and by projects such
as [acheong08/ChatGPT](https://github.com/acheong08/ChatGPT),
[gin337/ChatGPTReversed](https://github.com/gin337/ChatGPTReversed) and
[leetanshaj/openai-sentinel](https://github.com/leetanshaj/openai-sentinel).
The **Yandex Alice** adapter (`uniproxy` web-socket protocol) was written
without a known public source; corrections welcome.

## License

foxroute as a whole is distributed under **AGPL-3.0** — see
[LICENSE](LICENSE). It builds on copyleft code — gpt4free and deepseek4free
(GPL-3.0) and gemini-webapi (AGPL-3.0) — and follows the strongest of them,
AGPL-3.0, so the source stays open even when the gateway is run as a network
service.

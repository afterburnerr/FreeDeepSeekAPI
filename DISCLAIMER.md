# ⚠️ Disclaimer

**Read this before using FreeDeepSeekAPI.**

## What this project does

This software talks to the **undocumented web API** of
<https://chat.deepseek.com> on your behalf. It bypasses the browser
proof-of-work challenge and the AWS WAF clearance cookie so that an
OpenAI SDK or a `curl` script can drive the same backend that the
DeepSeek web UI uses. It is an independent project and is
**not affiliated with, sponsored by, or endorsed by DeepSeek**.

## What can go wrong

By running this software you accept, at minimum, the following risks:

1. **Your DeepSeek account can be suspended or permanently banned.**
   Automating access to `chat.deepseek.com` likely violates DeepSeek's
   Terms of Service. DeepSeek can detect unusual traffic patterns,
   non-browser clients, WAF-bypass behaviour and proof-of-work solvers
   and may disable your account without warning and without refund of
   any subscription.

2. **Your IP address may be rate-limited or blocked.** Edge protection
   (CloudFlare / AWS WAF) can blanket-ban an address that solves too
   many challenges. This can affect other users on the same network.

3. **DeepSeek can change or remove their API at any time.** The project
   may stop working on any given day with no notice. There is no
   support channel, no SLA, and no guarantee of continuity.

4. **Authentication tokens and cookies are sensitive.** The
   `DEEPSEEK_AUTH_TOKEN` and `dsk/cookies.json` in your deployment are
   equivalent to a logged-in session on your DeepSeek account. Treat
   them like passwords — never commit, never share, rotate immediately
   if leaked.

5. **Quality, safety and accuracy of responses are not guaranteed.** No
   output from this proxy should be relied on for anything that
   requires correctness (medical, legal, financial, safety-critical,
   production code without review). You are responsible for
   independently verifying all results.

## Forbidden uses

Do **not** use this software to:

- Build a commercial service that resells DeepSeek access or disguises
  the underlying provider.
- Circumvent paid API limits in a way intended to defraud DeepSeek.
- Scrape or archive large volumes of generated content in violation of
  copyright or DeepSeek's ToS.
- Spam, harass, generate disinformation, impersonate others, produce
  illegal content, or perform any activity prohibited by DeepSeek's
  usage policies or by applicable law.
- Build any system that processes personal data or regulated data
  (medical, financial, children) without your own compliance review.

## Recommendations

- **Use a throwaway DeepSeek account.** Do not sign up with your main
  email. Assume the account will be banned at some point.
- **Run behind a proxy or VPN** if you care about IP reputation on your
  home/office network.
- **Stay within reasonable request rates.** This project's session-pool
  and rotation defaults are tuned to look less robotic, but they are
  not a cloak of invisibility.
- **Follow DeepSeek's
  [Terms of Service](https://chat.deepseek.com/legal)** where they
  conflict with anything here; this project does not override them.
- **Review local law.** Automated access to online services is
  regulated differently in different jurisdictions. Check yours.

## No warranty, no liability

This software is provided **"AS IS"**, without warranty of any kind,
express or implied, as stated in the [LICENSE](LICENSE) (MIT).

The authors and contributors, including
[@afterburnerr](https://github.com/afterburnerr) and the upstream
authors of `deepseek4free` and `FreeDeepSeekAPI`, accept **no liability
whatsoever** for:

- banned or suspended DeepSeek accounts,
- IP-address blocks,
- financial loss, lost time, lost data, lost reputation,
- incorrect or harmful model output,
- breach of third-party terms of service that result from your use,
- any consequential or indirect damages.

**If you run this software, you do so at your own risk, and you take
sole responsibility for any consequences.**

## No affiliation

DeepSeek, the DeepSeek logo, `chat.deepseek.com` and all associated
trademarks are the property of their respective owners. This project is
a third-party client and is **not** an official DeepSeek product.

---

If any clause of this disclaimer conflicts with mandatory law in your
jurisdiction, that clause is severable and the rest still applies.

*Last updated: 2026.*

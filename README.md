# linkedin-mcp-server

An MCP server that publishes posts to your own LinkedIn feed through LinkedIn's
official [Posts API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api).

Python standard library only. No dependencies, no scraping, no session cookies.

## Why this exists

Most community LinkedIn MCP servers work by replaying your `li_at` browser
session cookie. That violates LinkedIn's User Agreement and risks having your
account restricted. This one uses the documented OAuth API with the
`w_member_social` scope, which is self-serve and takes about ten minutes to set
up.

The tradeoff is honest: the official API can write, but it cannot read. There is
no self-serve endpoint for your feed, your connections, or arbitrary profiles, so
this server cannot do those things and no amount of work here would change that.

## Tools

| Tool | Purpose |
|---|---|
| `linkedin_check_access` | Verify posting actually works, and how many days the token has left |
| `linkedin_whoami` | Return your name and author URN. Read-only, see the caveat below |
| `linkedin_create_post` | Publish a post, with up to 20 optional images |
| `linkedin_edit_post` | Replace the text of a published post |
| `linkedin_delete_post` | Delete a post by its URN |

## Requirements

Python 3.9 or newer, and a LinkedIn account. That is the whole list.

## Setup

### 1. Create a LinkedIn app

At <https://www.linkedin.com/developers/apps>, create an app and associate it
with a LinkedIn **Company Page**.

The page is only the app's *publisher*. It does not become the author of
anything: posts made through this server go to your own feed as you. You do not
need to own the page.

If the Products tab shows products as unavailable behind a verification notice,
open the app's **Settings** tab, click **Verify**, then **Generate URL**, and
send that URL to the page's super admin to approve. The link is valid for 30
days; a super admin can approve it in about a minute.

Note that page verification (the badge on a company page) and app-to-page
verification (this handshake) are unrelated mechanisms with confusingly similar
names.

### 2. Add both products

On the app's **Products** tab, request both. Each is granted instantly with no
review:

- *Sign In with LinkedIn using OpenID Connect*
- *Share on LinkedIn*

The second one is the one that matters. Without it you get a token that can
identify you but cannot post.

### 3. Generate a token

Open the [token generator](https://www.linkedin.com/developers/tools/oauth/token-generator),
select the app, and check `openid`, `profile`, and `w_member_social`.

**If `w_member_social` is not offered as a checkbox, step 2 did not take.** That
scope list is rendered from the products the app actually holds, which makes it
the authoritative check. Reload and confirm the product before continuing.

### 4. Store the token

Write it to `~/.config/linkedin-mcp/token`, readable only by you:

```bash
mkdir -p ~/.config/linkedin-mcp && chmod 700 ~/.config/linkedin-mcp
read -rs LINKEDIN_TOKEN && \
  printf '%s' "$LINKEDIN_TOKEN" > ~/.config/linkedin-mcp/token && \
  chmod 600 ~/.config/linkedin-mcp/token && \
  unset LINKEDIN_TOKEN
```

`read -rs` keeps the token off your screen and out of shell history. It needs a
real terminal, so run it yourself rather than through an assistant.

The token deliberately lives outside the repository. Two alternatives, in
precedence order ahead of the file: `LINKEDIN_ACCESS_TOKEN` with the token
itself, or `LINKEDIN_TOKEN_CMD` with a command that prints it, such as a
password-manager read. A path adjacent to `server.py` named `.token` is also
honored as a fallback.

### 5. Register the server

Claude Code:

```bash
claude mcp add linkedin -s user -- python3 /path/to/linkedin-mcp-server/server.py
```

Claude Desktop, in `claude_desktop_config.json`:

```json
"mcpServers": {
  "linkedin": {
    "command": "python3",
    "args": ["/path/to/linkedin-mcp-server/server.py"]
  }
}
```

Then run `linkedin_check_access`. It should report `"verdict": "Ready to post."`

## Running from WSL under Claude Desktop

If the server lives in WSL and Claude Desktop runs on Windows, bridge the
boundary with `wsl.exe`:

```json
"mcpServers": {
  "linkedin": {
    "command": "C:\\Windows\\System32\\wsl.exe",
    "args": [
      "-d", "Ubuntu", "--",
      "python3", "/home/<you>/linkedin-mcp-server/server.py"
    ]
  }
}
```

Three things are easy to get wrong here:

- **Fully quit Claude Desktop before editing its config.** It rewrites that file
  with its live preferences on exit and will silently discard your edit.
- **Use the absolute path to `wsl.exe`.** Claude Desktop's spawn environment on
  Windows frequently lacks a usable `PATH`.
- **Do not wrap the command in `bash -lc`.** A login shell prints its MOTD to
  stdout ahead of the first JSON-RPC frame and breaks the handshake with no
  useful error. This is also why the server reads its token from a file rather
  than an environment variable: Windows environment variables do not cross into
  WSL without `WSLENV`, and the obvious workaround is the thing that breaks it.

Windows paths passed to `linkedin_create_post` are translated automatically, so
`C:\Users\<you>\Pictures\chart.png` resolves to
`/mnt/c/Users/<you>/Pictures/chart.png`.

## Attaching images

Pass an `images` array. One image renders as a single image post; two or more
render as a swipeable carousel, up to 20. Each entry takes a `path` and an
optional `alt_text`.

```json
{
  "text": "Shipped the thing.",
  "images": [{"path": "~/shots/before.png", "alt_text": "Terminal before"},
             {"path": "~/shots/after.png",  "alt_text": "Terminal after"}]
}
```

Every path is validated before any upload begins, so a typo in the third of
three images fails without leaving two orphaned assets on your account.

Uploading is two steps per image: `POST /rest/images?action=initializeUpload`
returns an upload URL and an image URN, then the bytes go to that URL by `PUT`.
Because `w_member_social` is write-only against `/rest/images`, the upload's own
`201` is the only confirmation available; there is no readable status to poll.

## The truncation guard

A long post is a single long JSON string inside a `tools/call`. If the model
writing that call runs out of output tokens mid-string, the harness still emits a
valid call with a shortened `text`, and nothing downstream can distinguish that
from a deliberately short post. This is the most likely way a post goes out
half-written.

So `linkedin_create_post` and `linkedin_edit_post` refuse text that reads as cut
off mid-thought, before publishing rather than after. Text is rejected when it:

- ends with `,` `;` `:`
- ends on a dangling word such as `and`, `the`, `to`, `that`, `with`
- leaves a `(`, `[`, or `{` unclosed, or a double quote unbalanced
- ends on a letter or digit with no closing punctuation

Endings that are legitimately bare are exempt: hashtags, URLs, and anything
ending in an emoji or other non-alphanumeric character.

It is a heuristic, so pass `allow_incomplete: true` when an ending is deliberate.
Both tools also report the published `characters` count, which is what lets you
notice a draft that went in at 1,800 characters and came out at 900.

## Editing a published post

`linkedin_edit_post` replaces a post's text. The new text replaces the old
entirely; there is no append.

Only `commentary`, `contentCallToActionLabel`, `contentLandingPage`,
`lifecycleState`, and `adContext` are updatable. **`content` is not among them**,
so images cannot be added, removed, or swapped after publishing. A post with the
wrong image has to be deleted and reposted.

LinkedIn marks edited posts as edited to everyone who sees them.

## Limits

- Access tokens expire after **60 days**. `linkedin_check_access` reports how
  many are left.
- 150 requests per day per member; 100,000 per day per application.
- Post text is capped at 3000 characters, alt text at 4086.
- Images must be JPG, PNG, or GIF, under 36,152,320 pixels.
- `LINKEDIN_VERSION` in `server.py` pins the API version header. LinkedIn sunsets
  versions about a year out; bump it if calls start failing with a deprecation
  error.

## Troubleshooting

**`linkedin_whoami` succeeding proves almost nothing.** It exercises only
`/v2/userinfo`, which needs `openid` and `profile`. A token with those scopes and
nothing else passes `whoami` cleanly while every write fails. Use
`linkedin_check_access` instead: it probes read and write separately.

**`403 ACCESS_DENIED`, `serviceErrorCode: 100`, `Not enough permissions to
access: partnerApi...External...`** means the token lacks `w_member_social`,
which means the app lacks the *Share on LinkedIn* product. The resource name
varies by endpoint but the cause is the same. Scopes are fixed when a token is
issued, so an existing token can never gain one; you need a fresh token after
adding the product.

**Changing the token needs no restart.** The token is re-read from disk on every
request. **Adding a tool does** need one, since clients cache the tool list from
`tools/list` at connection time.

**Posts arriving truncated are almost never this server's fault.** It rejects
over-long text rather than trimming it, and never shortens a post. If a published
post is shorter than drafted, the text was already cut off when it reached the
tool, most likely because the model writing the tool call ran out of output
tokens mid-string.

## Not supported

Video, documents, polls, and articles each need their own asset upload flow
before the post call. Reading your feed, searching people, and pulling arbitrary
profiles have no self-serve API at all; those require LinkedIn partner approval.

## License

MIT

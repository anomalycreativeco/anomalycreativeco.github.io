# MLS Studio — Studio Hub page

One-stop MLS photo delivery: pick a shoot folder on the Mac, choose the AutoHDR look and
enhancements, point it at a Frame.io folder. The page then

1. creates an AutoHDR photoshoot and uploads the files (full HDR pipeline for brackets / camera
   files, or "finished photos" mode that skips HDR),
2. waits for AutoHDR to finish, optionally runs a re-edit prompt on every photo,
3. downloads the high-res finished set to `<shoot>/_MLS Studio/High Res`,
4. resizes an MLS set to under 4 MB (same ladder as the `mls-resizing` skill) into
   `<shoot>/_MLS Studio/MLS`,
5. uploads both to Frame.io: `<output folder>/<Shoot name>/High Res` and `.../MLS`.

Any stage can be switched off, so it also works as "resize only" or "resize + upload".

It is a single Python file with no dependencies beyond the Python 3 and `sips` that ship with
macOS. Keys live in the macOS Keychain. Jobs survive a page reload and can be resumed after a
server restart.

## Run

```bash
python3 mls-studio/server.py --open
```

Then open <http://localhost:8765> (`--open` does it for you). Port: `MLS_STUDIO_PORT`.

Dry run (AutoHDR + Frame.io simulated, nothing uploaded, no credits spent):

```bash
python3 mls-studio/server.py --dry-run --open
```

## One-time setup (Settings button, top right)

**AutoHDR.** This page talks to the same AutoHDR external API that the AutoHDR MCP server is
built on (`external.realestatephotoediting.com/v1`), under your account's credits. Create an API
app at <https://new.autohdr.com/api> and paste its client ID + secret into Settings, then press
*Test AutoHDR*. The app asks for the `photoshoots:read photoshoots:write images:edit` scopes.

**Frame.io.** Frame.io V4 authenticates through Adobe. In
<https://developer.adobe.com/console> (signed in with the same Adobe ID you use for next.frame.io)
create a project, add the **Frame.io API**, and choose **OAuth Web App**:

- Default redirect URI: `https://localhost:8766/callback`
- Redirect URI pattern: `https://localhost:8766/callback$`

Adobe only accepts `https` redirect URIs, even on localhost, so the page runs a second listener on
port 8766 with a self-signed certificate (generated once into
`~/Library/Application Support/Anomaly Studio Hub/mls-studio/tls/`). The browser warns once
during sign-in; proceed, it is this app on your own Mac. Paste the client ID + secret into
Settings, press *Connect Frame.io*, sign in, then *Test Frame.io*.

Picking the output folder: the Deliver step shows a Frame.io-style browser (projects down the
left, breadcrumb on top, folder tiles and file thumbnails) with a Finder-style **Columns** toggle.
The folder you are in is the output folder. Or paste any `next.frame.io/project/…` project or
folder link into the box above it and press Return; it resolves to that folder.

*Server-to-Server* is only offered for Enterprise plans administered in the Adobe Admin
Console. The option is kept in Settings for that case; on Pro/Team plans it will not appear in the
Developer Console.

**Why not the Frame.io connector in claude.ai?** That connector ("Frame.io (Pipedream)") talks to
the legacy v2 API, which cannot see V4 accounts, so it always reports "no account IDs". It has been
disabled. Claude sessions use `frameio_mcp.py` instead (below), and Frame.io's official MCP server
is in private beta (request access in the forum thread "Introducing the Frame.io MCP Server").

## Slack delivery note

After every Frame.io upload the job posts to Slack: shoot name, counts, destination path, looks
used, links to the shoot folder / High Res / MLS, AutoHDR shoot id and credits. Set it up once in
Settings → Slack with an incoming webhook for #general (api.slack.com/apps → Create New App →
Incoming Webhooks → Add New Webhook to Workspace → #general), or a bot token with `chat:write`
plus a channel name. "Send a test message" posts a one-line check. A Slack failure is logged on
the job card and never fails the delivery.

## Using it as a team

Each editor runs the server on their own Mac (it has to see their local shoot folder) and does
the Settings once: the studio's AutoHDR client ID/secret (all credits bill the studio account),
their own Adobe sign-in for Frame.io (their Adobe ID must be a member of the Anomaly Creative
Frame.io account), and the shared Slack webhook. Everything stays in that Mac's Keychain. Keep the
`mls-studio` folder in a shared location (the hub repo) so everyone runs the same version.

## Naming a shoot

Every shoot is named from three fields so the whole team names things the same way:

    Client - Address_Shoot type        e.g.  RCH - 1208 Barcroft_Interior MLS

That name is the AutoHDR shoot, the Slack note and the hub card. In Frame.io the client is already the
project, so the same fields file the delivery the way the team already does by hand:

    <picked folder> / <Address> / <Shoot type> / High Res + MLS
    <picked folder> / <Address> / High Res + MLS                  (shoot type "MLS": the main listing shoot)

If the picked folder is already the address folder, no second address folder is made. Client and shoot
type suggest what has been used before (shoot type also offers MLS, Interior MLS, Exteriors, Twilights,
Drone, Reshoot, Exterior Reshoot, Interior Reshoot, Staging, Updated Exteriors); picking a Frame.io
project fills in the client last used with it. A folder already named to the standard fills all three fields when it is scanned. Run stays
disabled until all three are filled in.

## Re-edit with a different look

When a finished set should have been a different look, press **Re-edit…** on its job card, pick the
new look and tick the photos (thumbnails come from the local High Res copy). AutoHDR re-renders each
ticked photo from the original files with that look (its `style` transform), so nothing is uploaded
again. It runs as its own job: re-render, download, MLS resize, Frame.io, Slack.

- The first delivery is never touched. The re-edit lands beside it in `Re-edit 1 - <Look>` (then 2, 3…),
  both in `_MLS Studio/` on disk and inside the shoot's Frame.io folder (under the address, or under the
  shoot type when there is one), with its own client share link.
- Cost: the look's per-photo price times the photos ticked, charged by AutoHDR on submit; failed photos
  are refunded and keep their earlier look.
- A look applies to every ticked photo, interior or exterior. For a mixed set, run it once for the
  interiors and once for the exteriors.
- A resumed re-edit never resubmits a photo it already sent (a repeat would be charged again).
- The re-render starts from the original camera files (the brackets), never from the earlier edit. It
  lands as a new version in the photo's AutoHDR history; AutoHDR's download only ever returns a photo's
  *current* version and does not switch to the new one by itself, so MLS Studio makes the new version
  current, checks that AutoHDR agrees, and only then downloads. If AutoHDR still shows the earlier look
  the job stops with an error instead of delivering the first edit a second time.
- Anything done on top of the first look (AutoHDR's automatic camera removal, a prompt edit) is not part
  of a fresh render. The job card lists those photos so they can be checked.
- Re-edits made with 1.11 or 1.12 delivered the first edit again. Their job cards show **Fetch the
  re-edited photos**: it pulls the versions AutoHDR already made (no new charge), replaces the local
  copies, and uploads to `Re-edit N - <Look> (corrected)` in Frame.io with a new share link. Delete the
  uncorrected Frame.io folder by hand afterwards.

## Credits (AutoHDR)

- House looks (Classic, Lisa, V4 skies, twilights): upload and processing are free; **1 credit per
  photo** when the high-res set is downloaded. Re-downloads are free.
- Creator looks (Aura, Fuse, Brut, Kasl, Editorial): bill their own per-photo price when
  processing finishes, download or not. The dropdown shows the price.
- Re-edit prompt: 1 credit per photo, charged on submit; failed edits are refunded.
- Enhancements (grass, declutter, fireplace, TV screens) run at ingest with the shoot.

The run bar shows a per-photo estimate and the live balance before you press Run.

## Layout of the output

```
<shoot folder>/
  _MLS Studio/
    High Res/   finished AutoHDR JPEGs at full resolution
    MLS/        same photos, each under the size ceiling (default 3999 KB)

Frame.io <chosen output folder>/
  <Shoot name>/
    High Res/
    MLS/
```

Turn off "create a folder named after the shoot" to put `High Res` and `MLS` straight into the
chosen folder. Files that already exist in the Frame.io folder are skipped, so a resumed job does
not create duplicates.

## Resume / failure behaviour

- The job list keeps every job; each has a log, progress, and links to the local folders and the
  Frame.io folder.
- If the server is restarted mid-job the job shows *interrupted*; **Resume** picks up at the step
  it was on. A job interrupted during the AutoHDR upload restarts from "create shoot", because
  AutoHDR's upload links expire after 5 minutes.
- AutoHDR "failure" is re-checked once after 3 minutes (slow shoots sometimes recover).
- A photo that cannot get under the MLS ceiling even at the smallest rung is reported in the
  job card, not silently dropped.

## Frame.io for Claude sessions (`frameio_mcp.py`)

A stdio MCP server that reuses the sign-in stored by the page, so one Adobe credential serves both.
It is registered at user scope in `~/.claude.json` under the name `frameio`; move the folder and
update that path. Tools: whoami, list accounts/workspaces/projects, list a folder, resolve a
project + path to a folder id, create folder, upload file/folder (skips existing names), get file
with download link, download, list and create comments.

Manual registration if needed:

```bash
claude mcp add --scope user frameio -- /usr/bin/python3 "/path/to/mls-studio/frameio_mcp.py"
```

## Dropping it into the Studio Hub

The page is self-contained: `server.py` + `static/`. To mount it in the hub, copy the
`mls-studio/` folder into the hub project and either link to `http://localhost:8765` from the
hub navigation or run it as a hub service on its own port (`MLS_STUDIO_PORT`). The page uses the
hub's design tokens (`--ground`, `--sage`, `--rule` …) and the Anomaly masthead, so it matches
the other hub pages without changes.

Data lives in `~/Library/Application Support/Anomaly Studio Hub/mls-studio/` (`config.json`,
`jobs.json`); secrets live in Keychain under `anomaly-studio-hub.mls-studio`.

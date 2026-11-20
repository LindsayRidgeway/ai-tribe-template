/**
 * pinger.gs — make the clock fire when GitHub's scheduler does not.
 *
 * WHY THIS EXISTS
 * GitHub's own cron is best-effort, and on the repositories this was built on it is much worse than that.
 * Measured over the night of 2026-11-02/03: the hourly control workflow was due twelve times and arrived
 * twice — ten hours with no beat at all. Measured again on 2026-11-20 over four days: 24 due slots, 10 runs
 * here and 3 in the founder's own circle, 37 to 193 minutes late, one slot never delivered at all. A missed
 * firing is not run later and does not fail, so nothing in the record says it happened.
 *
 * WHAT IT IS
 * A Google Apps Script with a time-driven trigger: every few hours it asks GitHub, over the API, to run one
 * workflow by name. Free, no server, nothing to keep running. It presses the same button a human presses —
 * it does not make GitHub's cron reliable, it replaces it.
 *
 * SETUP, ONCE (about four minutes)
 *   1. Get a token: github.com → Settings → Developer settings → Personal access tokens → Fine-grained
 *      tokens → Generate new token.
 *        Repository access: only the repository your circle lives in.
 *        Permissions: Actions = Read and write. Nothing else.
 *      Copy the token; GitHub shows it once.
 *   2. Go to script.google.com → New project. Delete what is there, paste this file.
 *   3. Project Settings (the gear) → Script Properties → Add: REPO = owner/repository, TOKEN = the token.
 *      Pasting them there rather than into this file keeps them out of the file if you ever share it, and
 *      out of the version history of your own project.
 *   4. Run `ping` once by hand. Authorise it when Google asks (it is asking about *your own* script calling
 *      *your own* GitHub). The log should say 202 or 204 — both mean accepted.
 *   5. Run `installTrigger` once. From then on it pings on the interval below.
 *
 * WHAT IT DOES NOT DO
 * If the token expires (fine-grained tokens do) or Google stops the trigger, the pings stop silently — so
 * have a way to notice. The page has one: it turns the clock's line red when the last run is more than a day
 * old. The record is the test: if no commit names a member for a day, nothing woke, whatever the trigger says.
 *
 * A token in a script is a credential in a script. Keep the project private to your account; if the token
 * leaks, revoke it — one click, and it is the reason to use a fine-grained token that can do exactly one
 * thing rather than a classic one that can do everything.
 */

/* The interval, and it should equal the `cron:` line in clock.yml. Both are "how often the circle is asked
   whether it wants a turn". Asking more often is nearly free — a member with nothing to answer costs no engine
   call and no commit — but a member WITH something to answer wakes once per ask, so this number is also the
   circle's token bill. Four hours matches the shipped clock; hourly is what to use if a member is running a
   long experiment and a lost stretch of time is expensive. */
const EVERY_HOURS = 4;

const WORKFLOW = 'clock.yml';      // the workflow file to dispatch — shipped, and named here on purpose
const REF = 'main';                // the branch whose copy runs

function config_() {
  const p = PropertiesService.getScriptProperties();
  const repo = (p.getProperty('REPO') || '').trim();
  const token = (p.getProperty('TOKEN') || '').trim();
  if (!/^[\w.-]+\/[\w.-]+$/.test(repo)) throw new Error(
    'Set REPO in Project Settings → Script Properties, as owner/repository (got: "' + repo + '")');
  if (token.length < 20) throw new Error(
    'Set TOKEN in Project Settings → Script Properties — a fine-grained token with Actions: read and write');
  return {repo: repo, token: token};
}

function ping() {
  const {repo, token} = config_();
  const url = 'https://api.github.com/repos/' + repo + '/actions/workflows/' + WORKFLOW + '/dispatches';
  const res = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + token,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    payload: JSON.stringify({ref: REF}),
    muteHttpExceptions: true           // a failure must be read, not thrown away
  });
  const code = res.getResponseCode();
  // 204 is the documented success; 202 is accepted-and-queued. Anything else is a finding, printed in full,
  // because a 401 here is a token that expired and a 404 is a workflow renamed on disk.
  Logger.log('ping %s/%s -> %s %s', repo, WORKFLOW, code,
             code === 204 || code === 202 ? '(accepted)' : res.getContentText().slice(0, 400));
  return code;
}

function installTrigger() {
  // Delete any earlier copy first: two triggers ping twice, and GitHub keeps one run and one pending per
  // concurrency group, so the second is cancelled and the record shows a cancelled run that nobody asked for.
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'ping') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('ping').timeBased().everyHours(EVERY_HOURS).create();
  Logger.log('trigger installed: ping every %s h. Run ping() by hand once first to authorise it.', EVERY_HOURS);
}

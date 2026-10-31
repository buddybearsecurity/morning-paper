// Starts the Morning Paper workflow on time every morning.
// GitHub's own scheduler is best-effort and can run hours late; Cloudflare's cron triggers aren't.
//
// Setup (Cloudflare dashboard → Workers & Pages → Create → Hello World):
//   1. Paste this file in as the Worker's code.
//   2. Settings → Variables and Secrets:
//        GH_TOKEN      (Secret)  fine-grained token: this repo only, Actions: Read and write
//        GH_REPO       (Text)    your-username/your-repo
//        TIMEZONE      (Text)    e.g. America/Toronto
//        SEND_HOUR     (Text)    e.g. 5
//        WORKFLOW_FILE (Text)    optional, default morning-paper.yml
//        GH_REF        (Text)    optional, default main
//   3. Settings → Trigger Events → Cron Triggers: add the two UTC times that equal SEND_HOUR:05
//      in your timezone across DST. For 5:05 a.m. Eastern: "5 9 * * *" and "5 10 * * *".
//      Only the one that matches the local hour does anything.
//   4. Delete the `schedule:` block in .github/workflows/morning-paper.yml, or you'll get two papers a day.

export default {
  async scheduled(event, env, ctx) {
    const hour = Number(new Intl.DateTimeFormat("en-CA", {
      timeZone: env.TIMEZONE, hour: "numeric", hourCycle: "h23",
    }).format(new Date()));
    if (hour !== Number(env.SEND_HOUR)) return;

    const workflow = env.WORKFLOW_FILE || "morning-paper.yml";
    const r = await fetch(
      `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${workflow}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "morning-paper-scheduler",
        },
        body: JSON.stringify({ ref: env.GH_REF || "main" }),
      },
    );
    if (r.status !== 204) throw new Error(`GitHub dispatch failed: ${r.status} ${await r.text()}`);
  },
};

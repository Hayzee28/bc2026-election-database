const NOTION_API_VERSION = "2026-03-11";
const NOTION_API_ROOT = "https://api.notion.com/v1";

function textValue(value) {
  return String(value ?? "").trim().slice(0, 2000);
}

function richText(value) {
  const content = textValue(value);
  return content ? [{ type: "text", text: { content } }] : [];
}

async function notionRequest(fetchImpl, token, path, init) {
  const response = await fetchImpl(`${NOTION_API_ROOT}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      "Notion-Version": NOTION_API_VERSION,
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const detail = (await response.text()).slice(0, 500);
    throw new Error(`Notion API ${response.status}: ${detail || response.statusText}`);
  }
  return response.json();
}

export async function upsertNotionTask({
  token,
  dataSourceId,
  title,
  area,
  source,
  link,
  notes,
  fetchImpl = fetch,
}) {
  for (const [name, value] of Object.entries({ token, dataSourceId, title, area, source, link })) {
    if (!textValue(value)) throw new Error(`${name} is required`);
  }
  new URL(link);

  const query = await notionRequest(
    fetchImpl,
    token,
    `/data_sources/${encodeURIComponent(dataSourceId)}/query`,
    {
      method: "POST",
      body: JSON.stringify({
        filter: { property: "Task", title: { equals: textValue(title) } },
        page_size: 10,
      }),
    },
  );

  const existing = (query.results ?? []).find((page) => !page.in_trash && !page.archived);
  const sharedProperties = {
    Status: { select: { name: "Inbox" } },
    Area: { select: { name: textValue(area) } },
    "Source / Idea From": { rich_text: richText(source) },
    Link: { url: link },
    Notes: { rich_text: richText(notes) },
  };

  if (existing) {
    const page = await notionRequest(fetchImpl, token, `/pages/${existing.id}`, {
      method: "PATCH",
      body: JSON.stringify({ properties: sharedProperties }),
    });
    return { action: "updated", pageId: page.id };
  }

  const page = await notionRequest(fetchImpl, token, "/pages", {
    method: "POST",
    body: JSON.stringify({
      parent: { type: "data_source_id", data_source_id: dataSourceId },
      properties: {
        Task: { title: richText(title) },
        ...sharedProperties,
      },
    }),
  });
  return { action: "created", pageId: page.id };
}

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    if (!key?.startsWith("--") || argv[index + 1] === undefined) {
      throw new Error("Arguments must use --name value pairs");
    }
    values[key.slice(2)] = argv[index + 1];
  }
  return values;
}

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  const token = process.env.NOTION_TOKEN;
  if (!token) {
    console.log("Notion sync skipped: add the NOTION_TOKEN Actions secret.");
    process.exit(0);
  }
  const args = parseArgs(process.argv.slice(2));
  const result = await upsertNotionTask({
    token,
    dataSourceId: process.env.NOTION_DATA_SOURCE_ID,
    title: args.title,
    area: args.area,
    source: args.source,
    link: args.link,
    notes: args.notes,
  });
  console.log(`Notion task ${result.action}: ${result.pageId}`);
}

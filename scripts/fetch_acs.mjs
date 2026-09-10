#!/usr/bin/env node
/**
 * fetch_acs.mjs — Agent Case Share AI 新闻日报采集
 * 通道：MCP (https://mcp.agentcaseshare.cn/mcp) + 页面抓全文
 * key 从 ~/Library/Application Support/agent-case-share/config.json 读（不落明文）
 * 输出：stdout JSON 数组 [{title,url,slug,item_date,updated_at,content}]
 * 用法：node fetch_acs.mjs [--limit N]   （N 为拉取 digest 期数，默认 5）
 */
import { readFileSync, existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const MCP_URL = 'https://mcp.agentcaseshare.cn/mcp';
const WEB_BASE = 'https://agentcaseshare.cn';
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36';

function loadKey() {
  try {
    const p = join(homedir(), 'Library', 'Application Support', 'agent-case-share', 'config.json');
    if (existsSync(p)) return JSON.parse(readFileSync(p, 'utf8')).apiKey || '';
  } catch {}
  return '';
}

function parseDateFromSlug(slug) {
  const m = String(slug || '').match(/(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${m[1]}-${m[2]}-${m[3]}` : '';
}

function htmlToText(html) {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style[\s\S]*?<\/style>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

async function mcpCall(msg, sid) {
  const headers = { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' };
  const key = loadKey();
  if (key) headers.Authorization = `Bearer ${key}`;
  if (sid) headers['mcp-session-id'] = sid;
  let lastErr = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const res = await fetch(MCP_URL, { method: 'POST', headers, body: JSON.stringify(msg) });
      const newSid = res.headers.get('mcp-session-id') || sid;
      const out = [];
      for (const line of (await res.text()).split('\n')) {
        const t = line.trim();
        if (t.startsWith('data:')) {
          const p = t.slice(5).trim();
          if (p && p !== '[DONE]') { try { out.push(JSON.parse(p)); } catch {} }
        }
      }
      return [newSid, out];
    } catch (e) {
      lastErr = e;
      await new Promise((r) => setTimeout(r, 1000 * (attempt + 1)));
    }
  }
  throw lastErr || new Error('mcpCall failed');
}

async function fetchPageText(path) {
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const res = await fetch(WEB_BASE + path, { headers: { 'User-Agent': UA } });
      if (!res.ok) continue;
      return htmlToText(await res.text());
    } catch { }
    await new Promise((r) => setTimeout(r, 1000 * (attempt + 1)));
  }
  return '';
}

async function main() {
  const limitIdx = process.argv.indexOf('--limit');
  const limit = limitIdx >= 0 ? Math.min(parseInt(process.argv[limitIdx + 1] || '5', 10) || 5, 10) : 5;

  let sid = '';
  [sid] = await mcpCall({
    jsonrpc: '2.0', id: 1, method: 'initialize',
    params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'fetch-acs', version: '1.0' } },
  }, '');
  await mcpCall({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} }, sid);

  const [, res] = await mcpCall({
    jsonrpc: '2.0', id: 2, method: 'tools/call',
    params: { name: 'search_content', arguments: { q: 'AI', type: 'news', limit } },
  }, sid);

  const textBlob = res?.[0]?.result?.content?.find((c) => c.type === 'text')?.text || '';
  let items = [];
  try { items = JSON.parse(textBlob).items || []; } catch {}

  const out = [];
  for (const it of items.slice(0, limit)) {
    const slug = it.slug || '';
    const fullUrl = WEB_BASE + (it.url || `/news/${slug}`);
    let content = '';
    if (it.url) content = await fetchPageText(it.url);
    if (!content || content.length < 200) content = it.excerpt || '';
    out.push({
      title: it.title || '',
      url: fullUrl,
      slug,
      item_date: parseDateFromSlug(slug) || (it.updatedAt || '').slice(0, 10),
      updated_at: it.updatedAt || '',
      content: content.slice(0, 6000),
    });
  }
  process.stdout.write(JSON.stringify(out));
}

main().catch((e) => {
  process.stderr.write('fetch_acs error: ' + (e && e.message ? e.message : String(e)) + '\n');
  process.exit(1);
});
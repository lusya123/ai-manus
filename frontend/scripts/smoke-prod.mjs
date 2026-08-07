import { existsSync } from "node:fs";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { chromium } from "playwright-core";
import { preview } from "vite";

const HOST = "127.0.0.1";
const PORT = Number(process.env.SMOKE_PORT || 4173);
const BASE_URL = `http://${HOST}:${PORT}`;
const ROUTES_TO_VERIFY = ["/", "/chat", "/chat/claw"];

function findChromeExecutable() {
  const candidates = [
    process.env.CHROME_PATH,
    process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  ].filter(Boolean);

  return candidates.find((candidate) => existsSync(candidate));
}

function frontendConfigResponse(overrides = {}) {
  return {
    code: 0,
    msg: "success",
    data: {
      auth_provider: "none",
      sub2api_login_url: null,
      sub2api_console_url: null,
      sub2api_marketplace_url: null,
      sub2api_use_token_url: null,
      show_github_button: true,
      github_repository_url: "https://github.com/simpleyyt/ai-manus",
      google_analytics_id: null,
      claw_enabled: true,
      default_model: {
        id: "system-default",
        label: "System Default",
        model_name: "default",
        model_provider: "openai",
        api_base: null,
      },
      available_models: [
        {
          id: "smoke-model",
          label: "Smoke Model",
          model_name: "smoke-model-name",
          model_provider: "openai",
          api_base: null,
        },
      ],
      ...overrides,
    },
  };
}

async function installApiMocks(page, options = {}) {
  await page.routeWebSocket("**/api/v1/ws/sessions", (ws) => {
    ws.send(JSON.stringify({ op: "snapshot", sessions: [] }));
  });

  await page.routeWebSocket("**/api/v1/ws/chat", (ws) => {
    ws.onMessage((message) => {
      const frame = JSON.parse(String(message));
      const sessionId = frame.session_id || "smoke-session";

      if (frame.type === "join_session") {
        ws.send(JSON.stringify({
          type: "joined",
          session_id: sessionId,
          request_id: frame.id,
        }));
        return;
      }

      if (frame.type === "leave_session") {
        ws.send(JSON.stringify({
          type: "left",
          session_id: sessionId,
          request_id: frame.id,
        }));
        return;
      }

      if (frame.type === "chat") {
        options.onChatSubmission?.(frame);
        ws.send(JSON.stringify({
          type: "ack",
          request_id: frame.id,
          submission_id: frame.id,
          op: "chat",
          session_id: sessionId,
          ok: true,
        }));
        return;
      }

      if (frame.type === "stop_session") {
        ws.send(JSON.stringify({
          type: "stopped",
          session_id: sessionId,
          request_id: frame.id,
        }));
      }
    });
  });

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname === "/api/v1/config/frontend") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(frontendConfigResponse(options.configData)),
      });
      return;
    }

    if (url.pathname === "/api/v1/auth/me" && request.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          code: 0,
          msg: "success",
          data: {
            id: "smoke-user",
            fullname: "Smoke User",
            email: "smoke@example.test",
            role: "user",
            is_active: true,
            created_at: "2026-01-01T00:00:00Z",
            updated_at: "2026-01-01T00:00:00Z",
            auth_provider: "sub2api",
          },
        }),
      });
      return;
    }

    if (url.pathname === "/api/v1/sessions") {
      if (request.method() === "GET") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            code: 0,
            msg: "success",
            data: { sessions: [] },
          }),
        });
        return;
      }

      if (request.method() === "PUT") {
        options.onCreateSession?.(request.postDataJSON());
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            code: 0,
            msg: "success",
            data: { session_id: "smoke-session" },
          }),
        });
        return;
      }

      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: "event: sessions\ndata: {\"sessions\":[]}\n\n",
      });
      return;
    }

    if (url.pathname === "/api/v1/sessions/smoke-session" && request.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          code: 0,
          msg: "success",
          data: {
            session_id: "smoke-session",
            title: "Smoke session",
            status: "completed",
            events: [],
            is_shared: false,
            is_favorite: false,
            is_pinned: false,
            project_id: null,
            task_mode: "agent",
            model_config: null,
          },
        }),
      });
      return;
    }

    if (url.pathname === "/api/v1/projects" && request.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ code: 0, msg: "success", data: { projects: [] } }),
      });
      return;
    }

    if (url.pathname === "/api/v1/claw") {
      if (request.method() === "GET") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            code: 0,
            msg: "success",
            data: {
              id: "smoke-claw",
              user_id: "smoke-user",
              status: "stopped",
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:00:00Z",
            },
          }),
        });
        return;
      }

      if (request.method() === "DELETE") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ code: 0, msg: "success", data: {} }),
        });
        return;
      }
    }

    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ code: 404, msg: "not mocked", data: null }),
    });
  });
}

async function verifyEnterSubmits(browser, consoleErrors, pageErrors, requestFailures) {
  const routePath = "/ enter-submit";
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
  let createSessionBody = null;
  let chatSubmissionFrame = null;
  let resolveChatSubmission;
  const chatSubmissionReceived = new Promise((resolve) => {
    resolveChatSubmission = resolve;
  });
  await installApiMocks(page, {
    onCreateSession: (body) => {
      createSessionBody = body;
    },
    onChatSubmission: (frame) => {
      chatSubmissionFrame = frame;
      resolveChatSubmission();
    },
  });
  await page.addInitScript(() => {
    // Simulate the deployed HTTP IP origin, where randomUUID is unavailable
    // even though getRandomValues remains usable.
    Object.defineProperty(globalThis.crypto, "randomUUID", {
      configurable: true,
      value: undefined,
    });
    localStorage.setItem("manus_selected_model_id", "smoke-model");
  });

  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(`[${routePath}] ${message.text()}`);
    }
  });
  page.on("pageerror", (error) => {
    pageErrors.push(`[${routePath}] ${error.stack || error.message}`);
  });
  page.on("requestfailed", (request) => {
    requestFailures.push(
      `[${routePath}] ${request.method()} ${request.url()} ${request.failure()?.errorText || ""}`.trim(),
    );
  });

  const draft = "smoke enter submit";
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 30_000 });
  const chatEditor = page.locator('.chat-input-editor .ProseMirror[contenteditable="true"]');
  await chatEditor.fill(draft);
  await chatEditor.press("Enter");
  await page.waitForURL(`${BASE_URL}/chat/smoke-session`, { timeout: 20_000 });
  await page.waitForFunction(
    (expectedText) => document.body.innerText.includes(expectedText),
    draft,
    { timeout: 20_000 },
  );
  let chatSubmissionTimeout;
  try {
    await Promise.race([
      chatSubmissionReceived,
      new Promise((_, reject) => {
        chatSubmissionTimeout = setTimeout(
          () => reject(new Error("Timed out waiting for the Enter-triggered chat WebSocket frame")),
          20_000,
        );
      }),
    ]);
  } finally {
    clearTimeout(chatSubmissionTimeout);
  }

  if (createSessionBody?.model_config?.model_id !== "smoke-model") {
    throw new Error(`Selected model was not sent when creating a session: ${JSON.stringify(createSessionBody)}`);
  }
  if (chatSubmissionFrame?.type !== "chat" || chatSubmissionFrame?.session_id !== "smoke-session" || chatSubmissionFrame?.message !== draft) {
    throw new Error(`Initial chat message was not submitted over WebSocket: ${JSON.stringify(chatSubmissionFrame)}`);
  }
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(chatSubmissionFrame?.id || "")) {
    throw new Error(`Chat submission did not include an RFC 4122 v4 UUID: ${JSON.stringify(chatSubmissionFrame)}`);
  }

  const state = await page.evaluate(() => ({
    path: window.location.pathname,
    bodyText: document.body.innerText.slice(0, 500),
  }));
  await page.close();
  return state;
}

async function verifyExternalHandoff(browser, consoleErrors, pageErrors, requestFailures) {
  const routePath = "/ external-handoff";
  const handoffState = "smoke-external-auth-state";
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
  await installApiMocks(page, {
    configData: {
      auth_provider: "sub2api",
      sub2api_login_url: "https://accounts.example.test/login",
    },
  });
  await page.addInitScript((state) => {
    sessionStorage.setItem("sub2api_external_auth_state", state);
  }, handoffState);

  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(`[${routePath}] ${message.text()}`);
  });
  page.on("pageerror", (error) => {
    pageErrors.push(`[${routePath}] ${error.stack || error.message}`);
  });
  page.on("requestfailed", (request) => {
    requestFailures.push(
      `[${routePath}] ${request.method()} ${request.url()} ${request.failure()?.errorText || ""}`.trim(),
    );
  });

  await page.goto(
    `${BASE_URL}/?keep=yes#manus_access_token=smoke-access-secret&manus_model_id=smoke-model&state=${handoffState}`,
    { waitUntil: "domcontentloaded", timeout: 30_000 },
  );
  await page.waitForFunction(() => (
    window.location.search === "?keep=yes"
    && localStorage.getItem("access_token") === "smoke-access-secret"
    && localStorage.getItem("manus_selected_model_id") === "smoke-model"
  ), { timeout: 20_000 });

  const state = await page.evaluate(() => ({
    path: window.location.pathname,
    search: window.location.search,
    importedAccessToken: localStorage.getItem("access_token") === "smoke-access-secret",
    selectedModelId: localStorage.getItem("manus_selected_model_id"),
  }));
  await page.close();
  return state;
}

async function main() {
  const server = await preview({
    preview: {
      host: HOST,
      port: PORT,
      strictPort: true,
    },
  });

  const consoleErrors = [];
  const pageErrors = [];
  const requestFailures = [];
  const checkedRoutes = [];
  let browser;

  try {
    const executablePath = findChromeExecutable();
    browser = await chromium.launch({
      headless: true,
      ...(executablePath ? { executablePath } : {}),
    });

    for (const routePath of ROUTES_TO_VERIFY) {
      const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
      await installApiMocks(page);

      page.on("console", (message) => {
        if (message.type() === "error") {
          consoleErrors.push(`[${routePath}] ${message.text()}`);
        }
      });
      page.on("pageerror", (error) => {
        pageErrors.push(`[${routePath}] ${error.stack || error.message}`);
      });
      page.on("requestfailed", (request) => {
        requestFailures.push(
          `[${routePath}] ${request.method()} ${request.url()} ${request.failure()?.errorText || ""}`.trim(),
        );
      });

      await page.goto(`${BASE_URL}${routePath}`, { waitUntil: "domcontentloaded", timeout: 30_000 });
      await page.waitForFunction(
        () => {
          const app = document.querySelector("#app");
          const bodyText = document.body.innerText.trim();
          if (!app || app.childElementCount === 0 || bodyText.length === 0) {
            return false;
          }
          if (window.location.pathname === "/chat/claw") {
            return bodyText.includes("OpenClaw");
          }
          return true;
        },
        { timeout: 20_000 },
      );

      checkedRoutes.push(
        await page.evaluate(() => {
          const app = document.querySelector("#app");
          return {
            path: window.location.pathname,
            title: document.title,
            readyState: document.readyState,
            bodyText: document.body.innerText.slice(0, 500),
            appChildCount: app?.childElementCount ?? 0,
          };
        }),
      );

      await page.close();
    }

    checkedRoutes.push(await verifyEnterSubmits(browser, consoleErrors, pageErrors, requestFailures));
    checkedRoutes.push(await verifyExternalHandoff(browser, consoleErrors, pageErrors, requestFailures));

    if (pageErrors.length || consoleErrors.length || requestFailures.length) {
      throw new Error(JSON.stringify({ checkedRoutes, pageErrors, consoleErrors, requestFailures }, null, 2));
    }

    console.log("Production frontend smoke test passed");
    console.log(JSON.stringify(checkedRoutes, null, 2));
  } catch (error) {
    if (browser) {
      const [page] = browser.contexts()[0]?.pages() || [];
      if (page) {
        const screenshotPath = join(process.cwd(), "smoke-prod-failure.png");
        await page.screenshot({ path: screenshotPath, fullPage: false }).catch(() => {});
        await writeFile(
          join(process.cwd(), "smoke-prod-failure.json"),
          JSON.stringify({ checkedRoutes, pageErrors, consoleErrors, requestFailures }, null, 2),
        ).catch(() => {});
        console.error(`Saved smoke failure artifacts to ${screenshotPath}`);
      }
    }
    throw error;
  } finally {
    if (browser) {
      await browser.close();
    }
    await new Promise((resolve) => server.httpServer.close(resolve));
  }
}

await main();

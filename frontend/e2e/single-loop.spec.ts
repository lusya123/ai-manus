import { test, expect } from '@playwright/test'

const MOCKSERVER = process.env.MOCKSERVER_URL || 'http://localhost:8090'

test.skip(process.env.RUN_SINGLE_LOOP_E2E !== '1', 'Requires backend AGENT_FLOW=agent_loop')

test('single loop runs sandbox work, reports plan, and shows streamed final result', async ({ page }) => {
  const scenario = await page.request.post(`${MOCKSERVER}/mock/scenario`, {
    data: { file: 'single_loop_e2e.yaml' }
  })
  expect(scenario.ok()).toBeTruthy()

  await page.goto('/')
  const editor = page.locator('.chat-input-editor [contenteditable="true"]').first()
  await editor.click()
  await editor.pressSequentially('Run the single loop smoke')
  await editor.press('Enter')
  await page.waitForURL(/\/chat\//, { timeout: 30_000 })

  await expect(page.getByText('Run the single loop smoke command').first()).toBeVisible({ timeout: 60_000 })
  await expect(page.getByText(/Single loop E2E smoke finished after the real sandbox command/).first()).toBeVisible({ timeout: 90_000 })
  await expect(page.getByText('Task completed').first()).toBeVisible({ timeout: 30_000 })
})

test('single loop waits after verified work and resumes to a completed plan', async ({ page }) => {
  const scenario = await page.request.post(`${MOCKSERVER}/mock/scenario`, {
    data: { file: 'single_loop_wait_e2e.yaml' }
  })
  expect(scenario.ok()).toBeTruthy()

  await page.goto('/')
  const editor = page.locator('.chat-input-editor [contenteditable="true"]').first()
  await editor.click()
  await editor.pressSequentially('Verify then ask me')
  await editor.press('Enter')
  await page.waitForURL(/\/chat\//, { timeout: 30_000 })

  await expect(page.getByText('Which option do you want, A or B?').first()).toBeVisible({ timeout: 60_000 })
  const reply = page.locator('.chat-input-editor [contenteditable="true"]').first()
  await reply.click()
  await reply.pressSequentially('Option B')
  await reply.press('Enter')

  await expect(page.getByText(/Single loop wait resumed with option B/).first()).toBeVisible({ timeout: 90_000 })
  await expect(page.getByText('Task completed').first()).toBeVisible({ timeout: 30_000 })
})

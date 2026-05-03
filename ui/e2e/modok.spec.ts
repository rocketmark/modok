import { test, expect } from '@playwright/test'

// All E2E modok tests run with MODOK_MOCK=1 (set in playwright.config.ts webServer command).

// @spec DEMO-MODOK-004, DEMO-BRIDGE-014
test('clicking Build Debug Packet renders the debug packet', async ({ page }) => {
  await page.goto('/tickets/ACME-1842')
  await page.getByRole('button', { name: /build debug packet/i }).click()
  await expect(page.getByTestId('debug-packet')).toBeVisible({ timeout: 15000 })
  await expect(page.getByTestId('debug-packet')).toContainText(/checkout/i)
})

// @spec DEMO-MODOK-011
test('debug packet is visible on page load when a completed run exists', async ({ page }) => {
  // First run to create completed state
  await page.goto('/tickets/ACME-1842')
  await page.getByRole('button', { name: /build debug packet/i }).click()
  await expect(page.getByTestId('debug-packet')).toBeVisible({ timeout: 15000 })

  // Reload and verify packet is still shown without clicking again
  await page.reload()
  await expect(page.getByTestId('debug-packet')).toBeVisible()
})

// @spec DEMO-CFG-004
test('shows full-panel error banner when config is missing', async ({ page }) => {
  // This test requires a server variant without config.json.
  // Marked for manual verification with a fixture server.
  test.skip()
})

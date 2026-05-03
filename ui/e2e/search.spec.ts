import { test, expect } from '@playwright/test'

// @spec DEMO-SEARCH-001
test('submitting a search query shows results in an overlay', async ({ page }) => {
  await page.goto('/tickets/ACME-1842')
  await page.getByRole('searchbox').fill('checkout')
  await page.getByRole('searchbox').press('Enter')
  await expect(page.getByTestId('search-overlay')).toBeVisible({ timeout: 5000 })
})

// @spec DEMO-SEARCH-003
test('search overlay closes on Escape', async ({ page }) => {
  await page.goto('/tickets/ACME-1842')
  await page.getByRole('searchbox').fill('checkout')
  await page.getByRole('searchbox').press('Enter')
  await expect(page.getByTestId('search-overlay')).toBeVisible({ timeout: 5000 })
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('search-overlay')).not.toBeVisible()
})

// @spec DEMO-SEARCH-003
test('search overlay closes when navigating to a ticket', async ({ page }) => {
  await page.goto('/tickets/ACME-1842')
  await page.getByRole('searchbox').fill('checkout')
  await page.getByRole('searchbox').press('Enter')
  await expect(page.getByTestId('search-overlay')).toBeVisible({ timeout: 5000 })
  await page.getByTestId('ticket-card-GLOBEX-991').click()
  await expect(page.getByTestId('search-overlay')).not.toBeVisible()
})

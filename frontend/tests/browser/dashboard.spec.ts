import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

test('unauthenticated dashboard is keyboard-accessible and has no serious axe findings', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: /sign in/i })).toBeVisible()
  await page.keyboard.press('Tab')
  await expect(page.locator(':focus')).toBeVisible()
  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations.filter((item) => ['critical', 'serious'].includes(item.impact ?? ''))).toEqual([])
})

import { test, expect } from '@playwright/test'

const BASE = 'https://elstrom.sparrenlab.se'

test.describe('elstrom.sparrenlab.se smoke tests', () => {

  test('startsida laddar och visar innehåll', async ({ page }) => {
    await page.goto(BASE)
    // Gated app — ska ladda utan serverfel
    await expect(page).toHaveTitle(/.+/)
    await expect(page.locator('body')).toBeVisible()
  })

  test('manifest.json finns', async ({ page }) => {
    const res = await page.goto(`${BASE}/manifest.json`)
    expect(res?.status()).toBe(200)
    const body = await res?.json()
    expect(body).toHaveProperty('name')
  })

  test('sw.js finns', async ({ page }) => {
    const res = await page.goto(`${BASE}/sw.js`)
    expect(res?.status()).toBe(200)
  })

  test('/architecture svarar', async ({ page }) => {
    const res = await page.goto(`${BASE}/architecture`)
    expect(res?.status()).toBeLessThan(500)
  })

  test('/api/status svarar och rapporterar env_ok', async ({ page }) => {
    const res = await page.goto(`${BASE}/api/status`)
    expect(res?.status()).toBe(200)
    const body = await res?.json()
    expect(body).toHaveProperty('env_ok', true)
  })

  test('/api/energy returnerar data', async ({ page }) => {
    const res = await page.goto(`${BASE}/api/energy?days=1`)
    expect(res?.status()).toBe(200)
    const body = await res?.json()
    expect(body).toHaveProperty('obj')
  })

  test('/api/prices returnerar data', async ({ page }) => {
    const res = await page.goto(`${BASE}/api/prices`)
    expect(res?.status()).toBeLessThan(500)
  })

  test('cron-jobben rapporterar inga fel i /api/status', async ({ page }) => {
    const res = await page.goto(`${BASE}/api/status`)
    expect(res?.status()).toBe(200)
    const body = await res?.json()
    const crons: Record<string, { last_ok: boolean; last_error: string | null }> =
      body?.cron_health ?? {}
    for (const [name, health] of Object.entries(crons)) {
      expect(health.last_ok, `Cron "${name}" reported failure: ${health.last_error}`).toBe(true)
    }
  })

  test('inga konsolfel på startsidan', async ({ page }) => {
    const errors: string[] = []
    page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()) })
    await page.goto(BASE)
    await page.waitForTimeout(2000)
    expect(errors).toHaveLength(0)
  })

})

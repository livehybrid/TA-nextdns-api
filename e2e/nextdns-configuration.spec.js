/*
 * Configuration page of TA-nextdns-api, driven in a browser on a real Splunk.
 * Each save is read back from splunkd's raw configs/conf-* endpoint and the
 * API key from storage/passwords, neither of which the page or the add-on's
 * REST handlers read. Keys are dummies; the build points the add-on at the
 * compose mock (local/nextdns_api_base), never api.nextdns.io.
 */
const path = require('path');
const { test, expect } = require('@playwright/test');
const ucc = require('./ucc/ucc-helpers');

const GC = require(path.join(__dirname, '..', 'globalConfig.json'));
const APP = GC.meta.name;
const ROOT = GC.meta.restRoot.toLowerCase();
const SETTINGS_CONF = `${ROOT}_settings`;
const ACCOUNT_CONF = `${ROOT}_account`;
const PAGE_TITLE = GC.pages.configuration.title;
const accountTab = GC.pages.configuration.tabs.find((t) => t.name === 'account');
const loggingTab = GC.pages.configuration.tabs.find((t) => t.name === 'logging');
const entity = (field) => accountTab.entity.find((e) => e.field === field);

// Visible label -> conf key, written out rather than looked up by field in
// globalConfig.json, so a remapped field cannot move the lookup with it.
const ACCOUNT = { name: 'Name', api_key: 'API key' };
const LOGGING = { loglevel: 'Log level' };

let rest;
let savedLogging;

test.beforeAll(async () => {
    rest = await ucc.restClient();
    savedLogging = await rest.confStanza(APP, SETTINGS_CONF, 'logging');
});

test.afterAll(async () => {
    await rest.setConf(APP, SETTINGS_CONF, 'logging', { loglevel: (savedLogging && savedLogging.loglevel) || 'INFO' });
    await rest.dispose();
});

test.beforeEach(async ({ page }) => {
    await ucc.login(page);
});

async function openAccounts(page) {
    await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
    await ucc.openTab(page, accountTab.title);
}

test('Configuration page loads every tab without app errors', async ({ page }) => {
    const errs = ucc.watchAppErrors(page, APP);
    await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
    for (const t of GC.pages.configuration.tabs) {
        await ucc.openTab(page, t.title);
        if (t.table) {
            await expect(page.getByRole('button', { name: 'Add', exact: true })).toBeVisible();
        } else {
            for (const e of t.entity) await expect(ucc.controlGroup(page, e.label)).toBeVisible();
        }
    }
    errs.assertNone();
});

test('Logging tab saves the chosen level and it survives a reload', async ({ page }) => {
    await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
    await ucc.openTab(page, loggingTab.title);
    const target = savedLogging && savedLogging.loglevel === 'DEBUG' ? 'WARNING' : 'DEBUG';
    await ucc.selectOption(page, page, LOGGING.loglevel, target);
    expect(await ucc.saveConfigTab(page)).toBe(200);
    await ucc.expectConf(() => rest.confStanza(APP, SETTINGS_CONF, 'logging'),
        (c) => c && c.loglevel === target, `[logging] loglevel=${target} after Save`);
    await page.reload();
    await ucc.openTab(page, loggingTab.title);
    await expect(ucc.controlGroup(page, LOGGING.loglevel)).toContainText(target);
});

test('account: create, edit and delete; the API key never comes back and lives in storage/passwords', async ({ page }) => {
    const errs = ucc.watchAppErrors(page, APP);
    const name = `e2e_acct_${Date.now().toString(36)}`;
    // Dummy, unique per run so a hit in the DOM cannot be anything else.
    const secret = `e2e-dummy-key-${Math.random().toString(36).slice(2)}`;
    const secret2 = `e2e-dummy-key2-${Math.random().toString(36).slice(2)}`;
    const check = (s) => ({ app: APP, conf: ACCOUNT_CONF, stanza: name, field: 'api_key', secret: s,
        handlerPath: `/servicesNS/nobody/${APP}/${ROOT}_account/${name}` });
    const stanza = () => rest.confStanza(APP, ACCOUNT_CONF, name);
    try {
        await openAccounts(page);

        await page.getByRole('button', { name: 'Add', exact: true }).click();
        const d = ucc.dialog(page);
        await expect(d).toBeVisible();
        await ucc.fillText(d, ACCOUNT.name, name);
        await ucc.fillText(d, ACCOUNT.api_key, secret);
        await ucc.submitDialog(page, 'Add');
        await expect(ucc.inputRow(page, name)).toBeVisible();
        await ucc.expectConf(stanza, (c) => c !== null, `[${name}] created`);
        await ucc.expectSecretNotEchoed(page, rest, check(secret));

        await page.reload();
        await ucc.openTab(page, accountTab.title);
        await expect(ucc.inputRow(page, name)).toBeVisible();
        expect.soft(await ucc.domHolds(page, secret), 'API key in the DOM after reload').toBe(false);

        // Edit with nothing changed: the mask the dialog shows must not replace the key.
        await ucc.rowAction(page, name, 'Edit');
        let e = ucc.dialog(page);
        await expect(e).toBeVisible();
        expect.soft(await ucc.domHolds(page, secret), 'API key in the edit dialog').toBe(false);
        await ucc.submitDialog(page, 'Update');
        await ucc.expectSecretNotEchoed(page, rest, check(secret));

        // Edit with a new key: the new one is stored, neither is shown.
        await ucc.rowAction(page, name, 'Edit');
        e = ucc.dialog(page);
        await expect(e).toBeVisible();
        await ucc.fillText(e, ACCOUNT.api_key, secret2);
        await ucc.submitDialog(page, 'Update');
        await ucc.expectSecretNotEchoed(page, rest, check(secret2));
        await page.reload();
        await ucc.openTab(page, accountTab.title);
        expect.soft(await ucc.domHolds(page, secret2), 'new API key in the DOM after reload').toBe(false);

        await ucc.rowAction(page, name, 'Delete');
        await ucc.submitDialog(page, 'Delete');
        await expect(ucc.inputRow(page, name)).toHaveCount(0);
        await ucc.expectConf(stanza, (c) => c === null, `[${name}] removed`);
        await ucc.expectSecretGone(rest, { app: APP, conf: ACCOUNT_CONF, stanza: name });
        errs.assertNone();
    } finally {
        await rest.del(`/servicesNS/nobody/${APP}/${ROOT}_account/${name}`);
    }
});

test('account: an invalid name is refused and nothing is written', async ({ page }) => {
    const bad = '9_starts_with_a_digit';
    await openAccounts(page);
    await page.getByRole('button', { name: 'Add', exact: true }).click();
    const d = ucc.dialog(page);
    await ucc.fillText(d, ACCOUNT.name, bad);
    await ucc.fillText(d, ACCOUNT.api_key, 'e2e-dummy-refused');
    await d.getByRole('button', { name: 'Add', exact: true }).click();
    await expect(d.getByText(entity('name').validators[0].errorMsg).first()).toBeVisible();
    await page.waitForTimeout(3000);
    expect(await rest.confStanza(APP, ACCOUNT_CONF, bad)).toBeNull();
    await d.getByRole('button', { name: 'Cancel', exact: true }).click();
});

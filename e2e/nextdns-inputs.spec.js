/*
 * Inputs page of TA-nextdns-api, driven in a browser on a real Splunk:
 * create, disable, enable, edit and delete one input of every service in
 * globalConfig.json. After each step the input's stanza is read from
 * splunkd's raw configs/conf-inputs endpoint, which the page and the add-on's
 * REST handler never read.
 *
 * The account has a dummy key and the add-on is pointed at the compose mock,
 * so the Profile dropdown and an enabled input reach the mock, never
 * api.nextdns.io. The mock's "quietprofile" has no data, so these inputs do
 * not add events the pytest suite counts.
 */
const path = require('path');
const { test, expect } = require('@playwright/test');
const ucc = require('./ucc/ucc-helpers');

const GC = require(path.join(__dirname, '..', 'globalConfig.json'));
const APP = GC.meta.name;
const ROOT = GC.meta.restRoot.toLowerCase();
const PAGE_TITLE = GC.pages.inputs.title;
const ACCOUNT_NAME = `e2e_inputs_acct_${Date.now().toString(36)}`;
const PROFILE = 'quietprofile';
const PROFILE_LABEL = `${ACCOUNT_NAME} - Quiet Profile (${PROFILE})`;

// Visible label -> inputs.conf key per service, written out here, not looked up
// from globalConfig.json by field name.
const COMMON = { name: 'Name', interval: 'Interval', index: 'Index', account: 'Account to use', profile: 'Profile' };
const FIELDS = {
    NextDNS_API_Stats: COMMON,
    NextDNS_API_Stream: { ...COMMON, raw: 'All queries (raw)' },
};
const VALUES = {
    NextDNS_API_Stats: { create: { interval: '300', index: 'main' }, edit: { interval: '120' } },
    NextDNS_API_Stream: { create: { interval: '300', index: 'main' }, edit: { interval: '120' }, raw: true },
};

let rest;
test.beforeAll(async () => {
    rest = await ucc.restClient();
    const r = await rest.post(`/servicesNS/nobody/${APP}/${ROOT}_account`, { name: ACCOUNT_NAME, api_key: 'e2e-dummy-key' });
    expect([200, 201], `create account: ${r.status}`).toContain(r.status);
});
test.afterAll(async () => {
    await rest.del(`/servicesNS/nobody/${APP}/${ROOT}_account/${ACCOUNT_NAME}`);
    await rest.dispose();
});
test.beforeEach(async ({ page }) => ucc.login(page));

test('every input service has test values and labels', () => {
    const services = GC.pages.inputs.services.map((s) => s.name).sort();
    expect(services).toEqual(Object.keys(VALUES).sort());
    expect(services).toEqual(Object.keys(FIELDS).sort());
});

/** Text fields by label; Tab commits the Index combo box and closes its menu. */
async function fillTexts(d, label, values) {
    for (const [f, v] of Object.entries(values)) {
        await ucc.fillText(d, label(f), v);
        await ucc.control(d, label(f)).press('Tab');
    }
}

for (const service of GC.pages.inputs.services) {
    const label = (key) => FIELDS[service.name][key];
    const stanza = (name) => rest.inputStanza(APP, service.name, name);
    const vals = VALUES[service.name];

    async function fillCreate(page, d, name) {
        await fillTexts(d, label, { name, ...vals.create });
        await ucc.selectOption(page, d, label('account'), ACCOUNT_NAME);
        // The profile list comes from the add-on's handler, which asks the mock.
        await ucc.selectOption(page, d, label('profile'), PROFILE_LABEL);
        if (vals.raw) await ucc.setCheckbox(d, label('raw'), true);
    }

    test(`${service.name}: create, disable, enable, edit and delete through the page`, async ({ page }) => {
        const errs = ucc.watchAppErrors(page, APP);
        const name = `e2e_${service.name.toLowerCase()}_${Date.now().toString(36)}`;
        try {
            await ucc.gotoUccPage(page, APP, 'inputs', PAGE_TITLE);

            const d = await ucc.openCreateInput(page, service.title);
            await fillCreate(page, d, name);
            await ucc.submitDialog(page, 'Add');
            await expect(ucc.inputRow(page, name)).toBeVisible();
            const made = await ucc.expectConf(() => stanza(name), (c) => c !== null, `[${service.name}://${name}] created`);
            const want = { ...vals.create, account: ACCOUNT_NAME, profile: PROFILE };
            for (const [f, v] of Object.entries(want)) expect(made[f], `created ${f}`).toBe(v);
            if (vals.raw) expect(ucc.isTrue(made.raw), 'created raw ticked').toBe(true);
            expect(ucc.isTrue(made.disabled), 'a new input starts enabled').toBe(false);

            await ucc.toggleRow(page, name);
            await ucc.expectConf(() => stanza(name), (c) => c && ucc.isTrue(c.disabled), 'disabled after the switch');
            await ucc.toggleRow(page, name);
            await ucc.expectConf(() => stanza(name), (c) => c && !ucc.isTrue(c.disabled), 'enabled after the switch');

            await ucc.rowAction(page, name, 'Edit');
            const e = ucc.dialog(page);
            await expect(e).toBeVisible();
            for (const [f, v] of Object.entries(vals.create)) expect(await ucc.readText(e, label(f)), `edit shows ${f}`).toBe(v);
            await expect(ucc.controlGroup(e, label('account'))).toContainText(ACCOUNT_NAME);
            await expect(ucc.controlGroup(e, label('profile'))).toContainText(PROFILE);
            await fillTexts(e, label, vals.edit);
            await ucc.submitDialog(page, 'Update');
            const after = await ucc.expectConf(() => stanza(name),
                (c) => c && Object.entries(vals.edit).every(([f, v]) => c[f] === v), 'edited values saved');
            for (const [f, v] of Object.entries({ ...want, ...vals.edit })) expect(after[f], `after edit ${f}`).toBe(v);
            if (vals.raw) expect(ucc.isTrue(after.raw), 'after edit raw still ticked').toBe(true);

            await ucc.rowAction(page, name, 'Delete');
            await ucc.submitDialog(page, 'Delete');
            await expect(ucc.inputRow(page, name)).toHaveCount(0);
            await ucc.expectConf(() => stanza(name), (c) => c === null, `[${service.name}://${name}] removed`);
            errs.assertNone();
        } finally {
            await rest.deleteInput(APP, service.name, name);
        }
    });

    test(`${service.name}: an invalid name or interval is refused and nothing is written`, async ({ page }) => {
        const bad = '9_starts_with_a_digit';
        await ucc.gotoUccPage(page, APP, 'inputs', PAGE_TITLE);
        const d = await ucc.openCreateInput(page, service.title);
        await fillCreate(page, d, bad);
        await d.getByRole('button', { name: 'Add', exact: true }).click();
        const nameMsg = service.entity.find((x) => x.field === 'name').validators[0].errorMsg;
        await expect(d.getByText(nameMsg).first()).toBeVisible();

        const good = `e2e_badinterval_${Date.now().toString(36)}`;
        await fillTexts(d, label, { name: good, interval: '5000' });
        await d.getByRole('button', { name: 'Add', exact: true }).click();
        const rangeMsg = service.entity.find((x) => x.field === 'interval').validators
            .find((v) => v.type === 'number').errorMsg;
        await expect(d.getByText(rangeMsg).first()).toBeVisible();
        await page.waitForTimeout(3000);
        expect(await stanza(bad)).toBeNull();
        expect(await stanza(good)).toBeNull();
        await d.getByRole('button', { name: 'Cancel', exact: true }).click();
    });
}

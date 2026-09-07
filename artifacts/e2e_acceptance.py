import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:5001")
OUT = Path(__file__).with_name("e2e_acceptance_result.json")


def wait_options(page, name, value):
    page.locator(f"select[name='{name}'] option[value='{value}']").first.wait_for(timeout=15000, state="attached")


def submit(page, path, values, expected_fragment=None):
    page.goto(BASE + path)
    page.wait_for_load_state("networkidle")
    for name, value in values.items():
        locator = page.locator(f"[name='{name}']")
        if locator.count() == 0:
            raise AssertionError(f"missing field {name} on {path}")
        if locator.first.evaluate("el => el.tagName") == "SELECT":
            locator.first.select_option(str(value))
        else:
            locator.first.fill(str(value))
    page.locator("form button").click()
    page.wait_for_load_state("networkidle")
    if expected_fragment and expected_fragment not in page.url:
        raise AssertionError(f"unexpected redirect for {path}: {page.url}")


def main():
    result = {"pages": [], "writes": [], "validation": {}, "screenshot": ""}
    modules = [
        "communities", "buildings", "units", "houses", "people", "relations", "leases",
        "staff", "roles", "work-orders", "complaints", "notices", "visitors", "vehicles",
        "parking", "parking-uses", "devices", "inspections", "fees", "bills", "payments", "audit",
    ]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(BASE + "/auth/login")
        page.wait_for_load_state("networkidle")
        assert page.locator("input[name='csrf_token']").count() == 1
        page.locator("input[name='username']").fill("e2e-admin")
        page.locator("input[name='password']").fill("E2E-pass-123")
        page.locator("form button").click()
        page.wait_for_load_state("networkidle")
        assert page.url.endswith("/dashboard"), page.url

        for module in modules:
            response = page.goto(BASE + "/manage/" + module)
            page.wait_for_load_state("networkidle")
            assert response and response.status == 200, (module, response.status if response else None)
            assert page.locator("table").count() == 1, module
            result["pages"].append({"module": module, "status": response.status, "title": page.title()})

        # Exercise the actual management forms, including server-side redirects.
        page.goto(BASE + "/operations/building.save")
        page.wait_for_load_state("networkidle")
        wait_options(page, "community_id", "1")
        page.locator("select[name='community_id']").select_option("1")
        page.locator("input[name='name']").fill("E2E 23栋")
        page.locator("input[name='floors']").fill("25")
        page.locator("form.business-form button").click()
        page.wait_for_url("**/manage/buildings/**", timeout=10000)
        assert "/manage/buildings/" in page.url, (page.url, page.locator("body").inner_text()[:1000])
        building_id = page.url.rsplit("/", 1)[-1]
        result["writes"].append("building.save")

        page.goto(BASE + "/operations/unit.save")
        page.wait_for_load_state("networkidle")
        wait_options(page, "building_id", building_id)
        page.locator("select[name='building_id']").select_option(building_id)
        page.locator("input[name='name']").fill("3单元")
        page.locator("form.business-form button").click()
        page.wait_for_url("**/manage/units/**", timeout=10000)
        assert "/manage/units/" in page.url
        unit_id = page.url.rsplit("/", 1)[-1]
        result["writes"].append("unit.save")

        page.goto(BASE + "/operations/house.save")
        page.wait_for_load_state("networkidle")
        wait_options(page, "unit_id", unit_id)
        page.locator("select[name='unit_id']").select_option(unit_id)
        page.locator("input[name='room_no']").fill("311")
        page.locator("input[name='area']").fill("100")
        page.locator("select[name='usage']").select_option("residential")
        page.locator("select[name='occupancy']").select_option("vacant")
        page.locator("form.business-form button").click()
        page.wait_for_url("**/manage/houses/**", timeout=10000)
        assert "/manage/houses/" in page.url
        house_id = page.url.rsplit("/", 1)[-1]
        result["writes"].append("house.save")

        page.goto(BASE + "/operations/person.save")
        page.wait_for_load_state("networkidle")
        wait_options(page, "community_id", "1")
        page.locator("select[name='community_id']").select_option("1")
        page.locator("input[name='name']").fill("王五")
        page.locator("input[name='phone']").fill("13800000123")
        page.locator("form.business-form button").click()
        page.wait_for_url("**/manage/people/**", timeout=10000)
        assert "/manage/people/" in page.url
        result["writes"].append("person.save")

        page.goto(BASE + "/operations/order.create")
        page.wait_for_load_state("networkidle")
        wait_options(page, "house_id", house_id)
        page.locator("select[name='house_id']").select_option(house_id)
        page.locator("input[name='title']").fill("厨房漏水")
        page.locator("textarea[name='content']").fill("水龙头接口漏水")
        page.locator("select[name='type']").select_option("水电故障")
        page.locator("input[name='location']").fill("E2E 23栋3单元311")
        page.locator("input[name='contact_name']").fill("王五")
        page.locator("input[name='contact_phone']").fill("13800000123")
        page.locator("form.business-form button").click()
        page.wait_for_url("**/manage/work-orders/**", timeout=10000)
        assert "/manage/work-orders/" in page.url
        result["writes"].append("order.create")

        # Browser-native required validation must prevent an empty submission.
        page.goto(BASE + "/operations/person.save")
        page.wait_for_load_state("networkidle")
        assert page.locator("input[required], select[required]").count() > 0
        result["validation"]["required_fields"] = True

        page.goto(BASE + "/manage/work-orders")
        page.screenshot(path=str(Path(__file__).with_name("e2e-work-orders.png")), full_page=True)
        result["screenshot"] = str(Path(__file__).with_name("e2e-work-orders.png"))
        browser.close()
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

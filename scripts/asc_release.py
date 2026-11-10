#!/usr/bin/env python3
"""
PHÁT HÀNH APP STORE bằng App Store Connect API (chạy trong CI — repo CI giữ khoá, máy dev không có khoá).
BẢN GỐC: GameDevHub/build/ios/asc_release.py — đẩy sang repo CI bằng `gdh ios install-workflow`.

Đọc thư mục bundle (do `gdh ios release` dựng): appstore.json + screenshots/*.png (1320x2868) + iap_review.png.
Làm, theo thứ tự, idempotent (chạy lại an toàn):
  phiên bản (khớp build mới nhất VALID) · mô tả/từ khoá/URL · tên/tên phụ/privacy · danh mục · phân loại tuổi ·
  quyền nội dung · giá miễn phí + mọi quốc gia · ảnh chụp iPhone 6.9" · thông tin liên hệ cho reviewer (chép từ app mẫu) ·
  IAP (tạo, mô tả, giá, quốc gia, ảnh duyệt) · gắn build · gửi duyệt (nếu --submit và không có lỗi trước đó).
App Privacy (khai báo dữ liệu) KHÔNG có trong API công khai — phải điền trên web trước khi gửi duyệt.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

import jwt
import requests

API = "https://api.appstoreconnect.apple.com"
ERRORS = []


def token():
    now = int(time.time())
    return jwt.encode({"iss": os.environ["ASC_ISSUER_ID"], "iat": now, "exp": now + 1100, "aud": "appstoreconnect-v1"},
                      os.environ["ASC_KEY_P8"], algorithm="ES256", headers={"kid": os.environ["ASC_KEY_ID"], "typ": "JWT"})


S = requests.Session()


def call(method, path, body=None, params=None, ok=(200, 201, 204), quiet=False):
    S.headers["Authorization"] = "Bearer " + token()
    r = S.request(method, path if path.startswith("http") else API + path, json=body, params=params, timeout=120)
    if r.status_code not in ok:
        msg = f"{method} {path} -> {r.status_code}: {r.text[:900]}"
        if not quiet:
            print("  ! " + msg)
        raise RuntimeError(msg)
    return r.json() if r.content else {}


def get(path, **params):
    return call("GET", path, params=params or None)


def step(name, fn):
    print(f"== {name}")
    try:
        return fn()
    except Exception as e:  # ghi lỗi, làm tiếp các bước khác
        ERRORS.append(f"{name}: {e}")
        print(f"  LỖI {name}: {e}")
        return None


def rel(t, i):
    return {"data": {"type": t, "id": i}}


def upload(create_path, create_type, rel_name, rel_type, rel_id, file_path):
    data = open(file_path, "rb").read()
    body = {"data": {"type": create_type, "attributes": {"fileName": os.path.basename(file_path), "fileSize": len(data)},
                     "relationships": {rel_name: rel(rel_type, rel_id)}}}
    res = call("POST", create_path, body)["data"]
    for op in res["attributes"]["uploadOperations"]:
        headers = {h["name"]: h["value"] for h in op.get("requestHeaders", [])}
        chunk = data[op["offset"]:op["offset"] + op["length"]]
        r = requests.request(op["method"], op["url"], data=chunk, headers=headers, timeout=300)
        if r.status_code >= 300:
            raise RuntimeError(f"upload {file_path} -> {r.status_code} {r.text[:300]}")
    call("PATCH", f"{create_path}/{res['id']}", {"data": {"type": create_type, "id": res["id"], "attributes": {
        "uploaded": True, "sourceFileChecksum": hashlib.md5(data).hexdigest()}}})
    return res["id"]


def all_pages(path, **params):
    out, url, p = [], path, dict(params, limit=200)
    while url:
        j = call("GET", url, params=p)
        out += j.get("data", [])
        url, p = j.get("links", {}).get("next"), None
    return out


def cancel_inflight(app_id, editable):
    """
    Huỷ đợt gửi duyệt đang treo (WAITING_FOR_REVIEW / IN_REVIEW / UNRESOLVED_ISSUES) để thay build khác vào CÙNG bản ghi
    phiên bản (app chưa phát hành lần nào: đổi versionString được). Mất chỗ trong hàng chờ ⇒ chỉ chạy khi tác giả đồng ý.
    Chờ tới khi phiên bản về trạng thái soạn được (thường DEVELOPER_REJECTED), tối đa ~10 phút.
    """
    subs = get(f"/v1/apps/{app_id}/reviewSubmissions", **{"filter[platform]": "IOS",
               "filter[state]": "WAITING_FOR_REVIEW,IN_REVIEW,UNRESOLVED_ISSUES"})["data"]
    if not subs:
        print("  (không có đợt gửi nào đang treo)")
        return
    for sub in subs:
        print(f"  huỷ reviewSubmission {sub['id']} ({sub['attributes'].get('state')})")
        call("PATCH", f"/v1/reviewSubmissions/{sub['id']}", {"data": {"type": "reviewSubmissions", "id": sub["id"],
             "attributes": {"canceled": True}}})
    for _ in range(60):
        vers = get(f"/v1/apps/{app_id}/appStoreVersions", **{"filter[platform]": "IOS", "limit": 10})["data"]
        states = [v["attributes"]["appStoreState"] for v in vers]
        if any(s in editable for s in states):
            print(f"  phiên bản soạn được lại: {states}")
            return
        time.sleep(10)
    raise RuntimeError(f"huỷ xong mà phiên bản chưa soạn được sau 10 phút: {states}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle_dir")
    ap.add_argument("--submit", action="store_true")
    ap.add_argument("--build", default="",
                    help="số build phải dùng: chờ tới khi nó VALID (tối đa ~45 phút) thay vì lấy build VALID mới nhất")
    ap.add_argument("--replace-inflight", action="store_true",
                    help="huỷ đợt gửi đang chờ/đang duyệt rồi dùng lại chính bản ghi phiên bản đó cho build mới (tác giả phải đồng ý)")
    a = ap.parse_args()
    d = a.bundle_dir
    spec = json.load(open(os.path.join(d, "appstore.json"), encoding="utf-8"))
    bundle = spec["bundle_id"]
    loc_name = spec.get("locale", "en-US")

    app = get("/v1/apps", **{"filter[bundleId]": bundle})["data"][0]
    app_id = app["id"]
    print(f"app {app_id} {app['attributes']['name']}")

    # Build mới nhất đã xử lý xong — hoặc ĐÚNG số build được chỉ định (vừa tải lên thì Apple còn xử lý: chờ).
    q = {"filter[app]": app_id, "filter[processingState]": "VALID", "sort": "-uploadedDate", "limit": 5,
         "include": "preReleaseVersion"}
    if a.build:
        q["filter[version]"] = a.build
    builds = get("/v1/builds", **q)
    for _ in range(270 if a.build else 0):
        if builds["data"]:
            break
        time.sleep(10)
        builds = get("/v1/builds", **q)
    if not builds["data"]:
        sys.exit(f"Chưa có build VALID{' số ' + a.build if a.build else ''} (đang xử lý hoặc chưa tải lên).")
    build = builds["data"][0]
    pre = {i["id"]: i for i in builds.get("included", [])}
    build_ver = pre[build["relationships"]["preReleaseVersion"]["data"]["id"]]["attributes"]["version"]
    print(f"build {build['attributes']['version']} (version {build_ver})")

    editable = ("PREPARE_FOR_SUBMISSION", "READY_FOR_REVIEW", "DEVELOPER_REJECTED", "REJECTED", "METADATA_REJECTED", "INVALID_BINARY")
    if a.replace_inflight:
        step("huỷ đợt gửi đang chờ duyệt", lambda: cancel_inflight(app_id, editable))
        if ERRORS:   # huỷ hỏng thì dừng — đừng để bước sau tự tạo bản ghi phiên bản mới
            sys.exit("Dừng: " + ERRORS[-1][:600])
    vers = get(f"/v1/apps/{app_id}/appStoreVersions", **{"filter[platform]": "IOS", "limit": 10})["data"]
    ver = next((v for v in vers if v["attributes"]["appStoreState"] in editable), None)
    if ver is None:
        ver = call("POST", "/v1/appStoreVersions", {"data": {"type": "appStoreVersions", "attributes": {
            "platform": "IOS", "versionString": build_ver}, "relationships": {"app": rel("apps", app_id)}}})["data"]
    vid = ver["id"]

    def version_attrs():
        attrs = {"copyright": spec.get("copyright", "2026 XOI GAME"), "releaseType": "AFTER_APPROVAL"}
        if ver["attributes"]["versionString"] != build_ver:
            attrs["versionString"] = build_ver
        call("PATCH", f"/v1/appStoreVersions/{vid}", {"data": {"type": "appStoreVersions", "id": vid, "attributes": attrs}})
    step("phiên bản " + build_ver, version_attrs)

    locs = get(f"/v1/appStoreVersions/{vid}/appStoreVersionLocalizations")["data"]
    vloc = next((l for l in locs if l["attributes"]["locale"] == loc_name), None)

    def version_loc():
        attrs = {k: spec[s] for k, s in (("description", "description"), ("keywords", "keywords"),
                                         ("promotionalText", "promotional_text"), ("supportUrl", "support_url"),
                                         ("marketingUrl", "marketing_url")) if spec.get(s)}
        call("PATCH", f"/v1/appStoreVersionLocalizations/{vloc['id']}",
             {"data": {"type": "appStoreVersionLocalizations", "id": vloc["id"], "attributes": attrs}})
    step("mô tả + từ khoá", version_loc)

    infos = get(f"/v1/apps/{app_id}/appInfos")["data"]
    info = next((i for i in infos if i["attributes"].get("appStoreState", i["attributes"].get("state")) not in
                 ("READY_FOR_DISTRIBUTION", "READY_FOR_SALE")), infos[0])
    iid = info["id"]

    def info_loc():
        il = get(f"/v1/appInfos/{iid}/appInfoLocalizations")["data"]
        l = next(x for x in il if x["attributes"]["locale"] == loc_name)
        call("PATCH", f"/v1/appInfoLocalizations/{l['id']}", {"data": {"type": "appInfoLocalizations", "id": l["id"],
             "attributes": {"name": spec["name"], "subtitle": spec["subtitle"], "privacyPolicyUrl": spec["privacy_url"]}}})
    step("tên + tên phụ + privacy", info_loc)

    def category():
        call("PATCH", f"/v1/appInfos/{iid}", {"data": {"type": "appInfos", "id": iid, "relationships": {
            "primaryCategory": rel("appCategories", "GAMES"),
            "primarySubcategoryOne": rel("appCategories", spec.get("subcategory", "GAMES_PUZZLE")),
            "secondaryCategory": rel("appCategories", spec.get("secondary_category", "ENTERTAINMENT"))}}})
    step("danh mục", category)

    def age():
        decl = get(f"/v1/appInfos/{iid}/ageRatingDeclaration")["data"]
        enums = {"alcoholTobaccoOrDrugUseOrReferences", "contests", "gamblingSimulated", "horrorOrFearThemes",
                 "matureOrSuggestiveThemes", "medicalOrTreatmentInformation", "profanityOrCrudeHumor",
                 "sexualContentGraphicAndNudity", "sexualContentOrNudity", "violenceCartoonOrFantasy", "violenceRealistic",
                 "violenceRealisticProlongedGraphicOrSadistic", "gunsOrOtherWeapons"}
        attrs = {}
        for k, v in decl["attributes"].items():
            if k in enums:
                attrs[k] = "NONE"
            elif k == "advertising":
                attrs[k] = True          # game có quảng cáo
            elif isinstance(v, bool) or (v is None and k in ("gambling", "unrestrictedWebAccess", "lootBox",
                                                                "messagingAndChat", "userGeneratedContent", "parentalControls",
                                                                "ageAssurance", "healthOrWellnessTopics")):
                attrs[k] = False
        # Apple thêm câu hỏi mới theo thời gian (vd. socialMedia 2026) ⇒ báo thiếu thuộc tính nào thì điền "không" rồi thử lại.
        for _ in range(12):
            try:
                call("PATCH", f"/v1/ageRatingDeclarations/{decl['id']}",
                     {"data": {"type": "ageRatingDeclarations", "id": decl["id"], "attributes": attrs}}, quiet=True)
                return
            except RuntimeError as e:
                m = re.search(r"attribute '(\w+)'", str(e))
                if not m or m.group(1) in attrs:
                    raise
                attrs[m.group(1)] = "NONE" if m.group(1) in enums else False
                print(f"  + {m.group(1)}={attrs[m.group(1)]}")
        raise RuntimeError("phân loại tuổi: quá nhiều thuộc tính thiếu")
    step("phân loại tuổi", age)

    step("quyền nội dung", lambda: call("PATCH", f"/v1/apps/{app_id}", {"data": {"type": "apps", "id": app_id, "attributes": {
        "contentRightsDeclaration": "DOES_NOT_USE_THIRD_PARTY_CONTENT"}}}))

    territories = [t["id"] for t in all_pages("/v1/territories")]

    def price_free():
        pts = all_pages(f"/v1/apps/{app_id}/appPricePoints", **{"filter[territory]": "USA"})
        p0 = next(p for p in pts if float(p["attributes"]["customerPrice"]) == 0)
        call("POST", "/v1/appPriceSchedules", {"data": {"type": "appPriceSchedules", "relationships": {
            "app": rel("apps", app_id), "baseTerritory": rel("territories", "USA"),
            "manualPrices": {"data": [{"type": "appPrices", "id": "${p0}"}]}}},
            "included": [{"type": "appPrices", "id": "${p0}", "attributes": {"startDate": None},
                          "relationships": {"appPricePoint": rel("appPricePoints", p0["id"])}}]})
    step("giá miễn phí", price_free)

    def availability():
        try:
            call("POST", "/v2/appAvailabilities", {"data": {"type": "appAvailabilities",
                 "attributes": {"availableInNewTerritories": True},
                 "relationships": {"app": rel("apps", app_id), "territoryAvailabilities": {"data": [
                     {"type": "territoryAvailabilities", "id": f"${{t{i}}}"} for i in range(len(territories))]}}},
                 "included": [{"type": "territoryAvailabilities", "id": f"${{t{i}}}", "attributes": {"available": True},
                               "relationships": {"territory": rel("territories", t)}} for i, t in enumerate(territories)]},
                 quiet=True)
        except RuntimeError as e:
            if "409" in str(e):
                print("  (đã có, giữ nguyên)")
            else:
                raise
    step(f"mở {len(territories)} quốc gia", availability)

    def screenshots():
        shots = sorted(f for f in os.listdir(os.path.join(d, "screenshots")) if f.lower().endswith(".png"))
        sets = get(f"/v1/appStoreVersionLocalizations/{vloc['id']}/appScreenshotSets")["data"]
        sset = next((s for s in sets if s["attributes"]["screenshotDisplayType"] == "APP_IPHONE_67"), None)
        if sset is None:
            sset = call("POST", "/v1/appScreenshotSets", {"data": {"type": "appScreenshotSets",
                        "attributes": {"screenshotDisplayType": "APP_IPHONE_67"},
                        "relationships": {"appStoreVersionLocalization": rel("appStoreVersionLocalizations", vloc["id"])}}})["data"]
        olds = get(f"/v1/appScreenshotSets/{sset['id']}/appScreenshots")["data"]
        # Đủ ảnh, đúng tên, không cái nào FAILED ⇒ giữ nguyên (ảnh đang xử lý mà xoá đi tải lại là Apple xử lý lại từ đầu).
        if len(olds) == len(shots[:10]) and all(o["attributes"].get("fileName") == f and
                                                (o["attributes"].get("assetDeliveryState") or {}).get("state") != "FAILED"
                                                for o, f in zip(olds, shots[:10])):
            print("  (đã đủ ảnh, giữ nguyên)")
            return
        for old in olds:
            call("DELETE", f"/v1/appScreenshots/{old['id']}")
        for f in shots[:10]:
            upload("/v1/appScreenshots", "appScreenshots", "appScreenshotSet", "appScreenshotSets", sset["id"],
                   os.path.join(d, "screenshots", f))
            print("  + " + f)
    step("ảnh chụp iPhone 6.9\"", screenshots)

    def review_detail():
        tpl_app = get("/v1/apps", **{"filter[bundleId]": spec.get("review_contact_from", "com.xoigame.stickmanarcherduel")})["data"][0]
        contact = None
        for v in get(f"/v1/apps/{tpl_app['id']}/appStoreVersions", limit=5)["data"]:
            try:
                rd = get(f"/v1/appStoreVersions/{v['id']}/appStoreReviewDetail")["data"]
            except RuntimeError:
                continue
            if rd and rd["attributes"].get("contactEmail"):
                contact = {k: rd["attributes"][k] for k in ("contactFirstName", "contactLastName", "contactPhone", "contactEmail")}
                break
        if not contact:
            raise RuntimeError("không lấy được liên hệ reviewer từ app mẫu")
        attrs = dict(contact, demoAccountRequired=False, notes=spec.get("review_notes", ""))
        try:
            mine = get(f"/v1/appStoreVersions/{vid}/appStoreReviewDetail")["data"]
        except RuntimeError:
            mine = None
        if mine:
            call("PATCH", f"/v1/appStoreReviewDetails/{mine['id']}",
                 {"data": {"type": "appStoreReviewDetails", "id": mine["id"], "attributes": attrs}})
        else:
            call("POST", "/v1/appStoreReviewDetails", {"data": {"type": "appStoreReviewDetails", "attributes": attrs,
                 "relationships": {"appStoreVersion": rel("appStoreVersions", vid)}}})
    step("liên hệ reviewer", review_detail)

    iap_ids = []

    def iaps():
        existing = {i["attributes"]["productId"]: i for i in all_pages(f"/v1/apps/{app_id}/inAppPurchasesV2")}
        for it in spec.get("iaps", []):
            pid = it["product_id"]
            print(f"  IAP {pid}")
            iap = existing.get(pid) or call("POST", "/v2/inAppPurchases", {"data": {"type": "inAppPurchases", "attributes": {
                "name": it["reference_name"], "productId": pid, "inAppPurchaseType": it["type"],
                "reviewNote": it.get("review_note", "Unlocks in-game content. Open the shop from the main menu.")},
                "relationships": {"app": rel("apps", app_id)}}})["data"]
            iap_id = iap["id"]
            iap_ids.append(iap_id)
            locs_ = get(f"/v2/inAppPurchases/{iap_id}/inAppPurchaseLocalizations")["data"]
            if not any(l["attributes"]["locale"] == loc_name for l in locs_):
                call("POST", "/v1/inAppPurchaseLocalizations", {"data": {"type": "inAppPurchaseLocalizations", "attributes": {
                    "locale": loc_name, "name": it["display_name"], "description": it["description"]},
                    "relationships": {"inAppPurchaseV2": rel("inAppPurchases", iap_id)}}})
            try:
                pts = all_pages(f"/v2/inAppPurchases/{iap_id}/pricePoints", **{"filter[territory]": "USA"})
                pp = next(p for p in pts if abs(float(p["attributes"]["customerPrice"]) - float(it["price"])) < 0.001)
                call("POST", "/v1/inAppPurchasePriceSchedules", {"data": {"type": "inAppPurchasePriceSchedules",
                     "relationships": {"inAppPurchase": rel("inAppPurchases", iap_id), "baseTerritory": rel("territories", "USA"),
                                       "manualPrices": {"data": [{"type": "inAppPurchasePrices", "id": "${p}"}]}}},
                     "included": [{"type": "inAppPurchasePrices", "id": "${p}", "attributes": {"startDate": None},
                                   "relationships": {"inAppPurchasePricePoint": rel("inAppPurchasePricePoints", pp["id"])}}]})
            except RuntimeError as e:
                ERRORS.append(f"giá IAP {pid}: {e}")
            try:
                call("POST", "/v1/inAppPurchaseAvailabilities", {"data": {"type": "inAppPurchaseAvailabilities",
                     "attributes": {"availableInNewTerritories": True},
                     "relationships": {"inAppPurchase": rel("inAppPurchases", iap_id),
                                       "availableTerritories": {"data": [{"type": "territories", "id": t} for t in territories]}}}},
                     quiet=True)
            except RuntimeError as e:
                if "409" not in str(e):
                    ERRORS.append(f"quốc gia IAP {pid}: {e}")
            shot = os.path.join(d, "iap_review.png")
            if os.path.exists(shot):
                try:
                    has = get(f"/v2/inAppPurchases/{iap_id}/appStoreReviewScreenshot").get("data")
                except RuntimeError:
                    has = None
                if not has:
                    upload("/v1/inAppPurchaseAppStoreReviewScreenshots", "inAppPurchaseAppStoreReviewScreenshots",
                           "inAppPurchaseV2", "inAppPurchases", iap_id, shot)
    step("IAP", iaps)

    step("gắn build", lambda: call("PATCH", f"/v1/appStoreVersions/{vid}/relationships/build",
                                   {"data": {"type": "builds", "id": build["id"]}}))

    if not a.submit:
        print("\nKhông gửi duyệt (thiếu --submit).")
    elif ERRORS:
        print("\nKHÔNG gửi duyệt vì còn lỗi ở trên.")
    else:
        def wait_assets():
            """Ảnh vừa tải lên còn 'Asset is being processed' thì gửi duyệt bị 409 — chờ tối đa ~15 phút."""
            for _ in range(90):
                sets = get(f"/v1/appStoreVersionLocalizations/{vloc['id']}/appScreenshotSets")["data"]
                states = [(x["attributes"].get("assetDeliveryState") or {}).get("state")
                          for st in sets for x in get(f"/v1/appScreenshotSets/{st['id']}/appScreenshots")["data"]]
                if all(st == "COMPLETE" for st in states):
                    print(f"  ảnh xử lý xong ({len(states)})")
                    return
                if any(st == "FAILED" for st in states):
                    raise RuntimeError(f"Apple xử lý ảnh FAILED: {states}")
                time.sleep(10)
            raise RuntimeError("ảnh chưa xử lý xong sau 15 phút")
        step("chờ Apple xử lý ảnh", wait_assets)

        def submit():
            subs = get(f"/v1/apps/{app_id}/reviewSubmissions", **{"filter[platform]": "IOS", "filter[state]": "READY_FOR_REVIEW"})["data"]
            sub = subs[0] if subs else call("POST", "/v1/reviewSubmissions", {"data": {"type": "reviewSubmissions",
                                            "attributes": {"platform": "IOS"}, "relationships": {"app": rel("apps", app_id)}}})["data"]
            try:
                call("POST", "/v1/reviewSubmissionItems", {"data": {"type": "reviewSubmissionItems", "relationships": {
                    "reviewSubmission": rel("reviewSubmissions", sub["id"]), "appStoreVersion": rel("appStoreVersions", vid)}}})
            except RuntimeError as e:
                if "409" not in str(e) or "already" not in str(e).lower():
                    raise
            for iap_id in iap_ids:   # IAP đầu tiên đi cùng phiên bản
                try:
                    call("POST", "/v1/inAppPurchaseSubmissions", {"data": {"type": "inAppPurchaseSubmissions",
                         "relationships": {"inAppPurchaseV2": rel("inAppPurchases", iap_id)}}}, quiet=True)
                except RuntimeError as e:
                    print(f"  (IAP {iap_id} chưa gửi riêng được: {str(e)[:200]})")
            call("PATCH", f"/v1/reviewSubmissions/{sub['id']}", {"data": {"type": "reviewSubmissions", "id": sub["id"],
                 "attributes": {"submitted": True}}})
            print("  ĐÃ GỬI DUYỆT")
        step("gửi duyệt", submit)

    print("\n=== KẾT QUẢ ===")
    if ERRORS:
        for e in ERRORS:
            print(" - " + e[:600])
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()

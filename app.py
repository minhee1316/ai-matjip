import json
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st
from openai import OpenAI

st.set_page_config(page_title="AI 맛집 추천 서비스", layout="wide")

KST = timezone(timedelta(hours=9))
KAKAO_URL ="https://dapi.kakao.com/v2/local/search"

SYSTEM_PROMPT = """당신은 대학생의 점심 고민을 덜어주는 친절한 맛집 안내자입니다.
추천은 정확히 3곳, 추천 이유는 한 줄, 존댓말로 작성하세요.
제공된 식당 목록 안에서만 고르세요.
목록에 없는 정보(메뉴·가격·맛·분위기)는 추측하지 마세요. 이름, 카테고리, 거리와 사용자의 오늘 상황만 근거로 삼으세요.
반드시 다음 JSON 형식으로만 답하세요:
{"recommendations": [{"id": "식당 id", "reason": "추천 이유 한 줄"}]}"""


def secret(name):
    return st.secrets.get(name, "")


# ---------- 카카오 로컬 API ----------
def search_restaurants(place, radius, sort):
    headers = {"Authorization": f"KakaoAK {secret('KAKAO_REST_API_KEY')}"}
    r1 = requests.get(f"{KAKAO_URL}/keyword.json", headers=headers, params={"query": place}, timeout=10)
    r1.raise_for_status()
    docs = r1.json()["documents"]
    if not docs:
        return None
    x, y = docs[0]["x"], docs[0]["y"]

    results = []
    for page in range(1, 4):  # 카카오는 한 페이지 최대 15개, 최대 3페이지(45개)
        r2 = requests.get(
            f"{KAKAO_URL}/category.json",
            headers=headers,
            params={"category_group_code": "FD6", "x": x, "y": y, "radius": radius,
                    "sort": sort, "page": page, "size": 15},
            timeout=10,
        )
        r2.raise_for_status()
        data = r2.json()
        results += data["documents"]
        if data["meta"]["is_end"]:
            break
    return results


def food_type(r):
    parts = r["category_name"].split(" > ")
    return parts[1] if len(parts) > 1 else parts[0]


# ---------- OpenAI 추천 ----------
def recommend(situation, places):
    client = OpenAI(api_key=secret("OPENAI_API_KEY"))
    candidates = [
        {"id": p["id"], "name": p["place_name"], "category": p["category_name"], "distance_m": p["distance"]}
        for p in places
    ]
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"오늘 상황: {situation}\n후보 식당 목록: {json.dumps(candidates, ensure_ascii=False)}"},
        ],
        response_format={"type": "json_object"},
    )
    result = json.loads(response.choices[0].message.content)
    by_id = {p["id"]: p for p in places}
    # 목록에 없는 id는 버림 — AI가 식당을 지어낼 수 없게
    return [(by_id[r["id"]], r["reason"]) for r in result.get("recommendations", []) if r.get("id") in by_id]


# ---------- Supabase ----------
@st.cache_resource
def get_db():
    if not (secret("SUPABASE_URL") and secret("SUPABASE_KEY")):
        return None
    from supabase import create_client
    return create_client(secret("SUPABASE_URL"), secret("SUPABASE_KEY"))


db = get_db()


def save_favorite(place, nickname, memo):
    if not nickname.strip():
        st.warning("별칭을 입력해주세요")
        return
    exists = db.table("favorites").select("id").eq("place_id", place["id"]).execute().data
    if exists:
        st.info("이미 즐겨찾기에 저장된 식당이에요")
        return
    db.table("favorites").insert(
        {"place_id": place["id"], "place_url": place["place_url"], "nickname": nickname.strip(), "memo": memo.strip() or None}
    ).execute()
    st.success(f"'{nickname}' 저장했어요")


def place_card(p, reason=None):
    with st.container(border=True):
        st.markdown(f"**{p['place_name']}** · {food_type(p)} · {p['distance']}m")
        if reason:
            st.write(f"💡 {reason}")
        st.markdown(f"[카카오맵에서 보기]({p['place_url']})")
        if db:
            with st.popover("⭐ 즐겨찾기 저장"):
                nick = st.text_input("별칭 (필수)", key=f"nick_{p['id']}{'_ai' if reason else ''}")
                memo = st.text_input("메모 (선택)", key=f"memo_{p['id']}{'_ai' if reason else ''}")
                if st.button("저장", key=f"save_{p['id']}{'_ai' if reason else ''}"):
                    save_favorite(p, nick, memo)


# ---------- 화면 ----------
st.title("AI 맛집 추천 서비스")

if "results" not in st.session_state:
    st.session_state.results = None
    st.session_state.recs = None

with st.sidebar:
    st.header("필터")
    radius = st.slider("반경(m)", 100, 2000, 500, step=100)
    sort_label = st.selectbox("정렬", ["거리순", "정확도순"])
    sort = "distance" if sort_label == "거리순" else "accuracy"
    types = sorted({food_type(r) for r in st.session_state.results or []})
    chosen_type = st.selectbox("음식 종류", ["전체"] + types)

tab_search, tab_fav = st.tabs(["🔍 검색", "⭐ 즐겨찾기"])

with tab_search:
    col1, col2 = st.columns([4, 1], vertical_alignment="bottom")
    place = col1.text_input("어디서 먹을까요?", placeholder="예: 강남역")
    if col2.button("검색", use_container_width=True):
        if not place.strip():
            st.warning("위치를 입력해주세요")
        else:
            with st.spinner(f"{place} 주변을 검색합니다..."):
                try:
                    found = search_restaurants(place.strip(), radius, sort)
                except requests.RequestException as e:
                    found = []
                    st.error(f"카카오 API 오류: {e}")
            if found is None:
                st.error("장소를 찾지 못했어요. 다른 이름으로 검색해보세요")
            elif found:
                st.session_state.results = found
                st.session_state.recs = None
                st.rerun()  # 사이드바 음식 종류 목록 갱신

    results = st.session_state.results
    if results:
        shown = [r for r in results if chosen_type == "전체" or food_type(r) == chosen_type]
        st.metric("검색된 식당 수", len(shown))

        situation = st.text_input("오늘 상황", placeholder="예: 비 오는 날, 혼자, 빨리")
        if st.button("🤖 AI 추천"):
            if not shown:
                st.warning("추천할 식당이 없어요")
            else:
                with st.spinner("AI가 고르는 중..."):
                    try:
                        st.session_state.recs = recommend(situation or "특별한 상황 없음", shown)
                    except Exception as e:
                        st.error(f"AI 추천 오류: {e}")

        if st.session_state.recs:
            st.subheader("AI 추천 3곳")
            cols = st.columns(3)
            for c, (p, reason) in zip(cols, st.session_state.recs):
                with c:
                    place_card(p, reason)

        st.subheader("검색 결과")
        if not shown:
            st.info("조건에 맞는 식당이 없어요")
        for p in shown:
            place_card(p)

with tab_fav:
    if not db:
        st.info("secrets.toml에 SUPABASE_URL, SUPABASE_KEY를 넣으면 즐겨찾기를 쓸 수 있어요")
    else:
        favs = db.table("favorites").select("*").order("created_at", desc=True).execute().data
        if not favs:
            st.info("아직 저장한 즐겨찾기가 없어요")
        for f in favs:
            with st.container(border=True):
                st.markdown(f"**{f['nickname']}** · [카카오맵]({f['place_url']}) · {datetime.fromisoformat(f['created_at']).astimezone(KST):%Y-%m-%d %H:%M}")
                new_memo = st.text_input("메모", value=f["memo"] or "", key=f"fm_{f['id']}")
                c1, c2 = st.columns(2)
                if c1.button("메모 수정", key=f"up_{f['id']}"):
                    db.table("favorites").update({"memo": new_memo or None}).eq("id", f["id"]).execute()
                    st.rerun()
                if c2.button("삭제", key=f"del_{f['id']}"):
                    db.table("favorites").delete().eq("id", f["id"]).execute()
                    st.rerun()

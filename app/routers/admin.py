from collections import Counter, defaultdict

from fastapi import APIRouter, Depends

from app.security import require_admin
from app.services import firestore_repo

router = APIRouter()


@router.get("/admin/users")
def list_users(_admin: dict = Depends(require_admin)) -> dict:
    """Super Admin: directory of everyone registered on the chatbot."""
    users = firestore_repo.list_users()
    safe = [
        {
            "id": u["id"],
            "email": u.get("email", ""),
            "name": u.get("name", ""),
            "picture": u.get("picture", ""),
            "provider": u.get("provider", "google"),
            "created_at": u.get("created_at", ""),
            "last_login": u.get("last_login", ""),
        }
        for u in users
    ]
    return {"users": safe, "total": len(safe)}


@router.get("/admin/analytics")
def analytics(_admin: dict = Depends(require_admin)) -> dict:
    """Super Admin: month-on-month creative-request volume + breakdowns."""
    events = firestore_repo.list_creative_events()

    by_month: Counter[str] = Counter()
    by_brand: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    month_brand: dict[str, Counter[str]] = defaultdict(Counter)

    for ev in events:
        month = ev.get("year_month", "unknown")
        brand = ev.get("brand") or "Unbranded"
        category = ev.get("category") or "other"
        by_month[month] += 1
        by_brand[brand] += 1
        by_category[category] += 1
        month_brand[month][brand] += 1

    months = sorted(by_month.keys())
    return {
        "total_requests": len(events),
        "monthly": [
            {
                "month": m,
                "count": by_month[m],
                "by_brand": dict(month_brand[m]),
            }
            for m in months
        ],
        "by_brand": dict(by_brand.most_common()),
        "by_category": dict(by_category.most_common()),
    }

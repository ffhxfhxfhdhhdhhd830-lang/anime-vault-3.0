"""Preview or idempotently import workbook catalogue/watchlist rows."""
import argparse
import asyncio
from pathlib import Path
from openpyxl import load_workbook
from anime_bot.config import get_settings
from anime_bot.database.mongo import Mongo
from anime_bot.services.core import normalize

TITLE_COLUMNS=("title","anime","name","english title","series")
STATUS_MAP={"plan to watch":"plan","planned":"plan","plan":"plan","completed":"completed","watched":"completed",
            "watching":"watching","on hold":"on_hold","hold":"on_hold","dropped":"dropped","unknown":"unknown"}

def read_rows(path: Path):
    book=load_workbook(path,read_only=True,data_only=True)
    rows=[]
    for sheet in book.worksheets:
        iterator=sheet.iter_rows(values_only=True)
        headers=next(iterator,None)
        if not headers: continue
        keys=[str(x or "").strip().casefold() for x in headers]
        title_idx=next((keys.index(k) for k in TITLE_COLUMNS if k in keys),None)
        if title_idx is None: continue
        for number, values in enumerate(iterator,start=2):
            title=str(values[title_idx] or "").strip() if title_idx<len(values) else ""
            if not title: continue
            item={k:(values[i] if i<len(values) else None) for i,k in enumerate(keys) if k}
            status_raw=str(item.get("status") or item.get("watch status") or "").strip().casefold()
            rows.append({"title":title,"normalized":normalize(title),"status":STATUS_MAP.get(status_raw),"row":number,"sheet":sheet.title,"raw":item})
    return rows

async def run(path: Path, dry_run: bool):
    rows=read_rows(path); settings=get_settings()
    print(f"Rows with titles: {len(rows)}; invalid rows: 0")
    if len({x['normalized'] for x in rows})<len(rows): print(f"Duplicate title rows: {len(rows)-len({x['normalized'] for x in rows})}")
    if dry_run:
        for row in rows[:20]: print(f"{row['sheet']}!{row['row']}: {row['title']} | status={row['status'] or 'unspecified'}")
        print("Dry run only: database was not changed."); return
    settings.validate_runtime(); mongo=Mongo(settings)
    try:
        await mongo.start(); db=mongo.db; created=0; updated=0
        for row in rows:
            raw=row['raw']; year=raw.get("year") or raw.get("release year")
            genres=raw.get("genres") or raw.get("genre") or []
            if isinstance(genres,str): genres=[x.strip() for x in genres.replace(";",",").split(",") if x.strip()]
            fields={"title":row['title'],"title_norm":row['normalized'],"import_source":path.name}
            if year:
                try: fields["year"]=int(year)
                except (ValueError,TypeError): pass
            if genres: fields["genres"]=genres
            result=await db.anime.update_one({"title_norm":row['normalized']},{"$setOnInsert":{**fields,"format":"unknown"}},upsert=True)
            created+=int(result.upserted_id is not None)
            anime=await db.anime.find_one({"title_norm":row['normalized']})
            if row['status']:
                # Status is inserted only when there is no pre-existing personal record.
                await db.watchlist.update_one({"user_id":settings.owner_telegram_id,"anime_id":anime['_id']},{"$setOnInsert":{
                    "user_id":settings.owner_telegram_id,"anime_id":anime['_id'],"status":row['status'],"rating":None,"favourite":False,"priority":0,"notes":"","review":""}},upsert=True)
        await db.import_jobs.insert_one({"source":path.name,"rows":len(rows),"created":created,"at":__import__('datetime').datetime.now(__import__('datetime').timezone.utc)})
        print(f"Imported {len(rows)} rows; new catalogue entries: {created}. Existing statuses preserved.")
    finally: await mongo.close()

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("workbook",type=Path); parser.add_argument("--dry-run",action="store_true")
    args=parser.parse_args()
    if not args.workbook.is_file(): parser.error(f"Workbook not found: {args.workbook}")
    asyncio.run(run(args.workbook,args.dry_run))
if __name__=="__main__": main()

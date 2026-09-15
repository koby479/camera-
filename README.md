# Universal Cam Viewer

תוכנת צפייה אוניברסלית במצלמות אבטחה (ONVIF / RTSP) - תומכת גם ב-NVR (כולל Provision-ISR
עם קושחה שתומכת ONVIF G/T/S) וגם במצלמות בודדות (כולל מצלמות סיניות/Annke שתומכות RTSP/ONVIF).

## מבנה
- `app/core/camera.py` - מודל מצלמה + thread שמושך RTSP ומדווח סטטוס (פעילה/מתה/לא מחוברת)
- `app/core/discovery.py` - שאילתות ONVIF מול NVR כדי להביא את כל הערוצים שלו אוטומטית
- `app/core/store.py` - שמירת רשימת ה-NVR-ים והמצלמות הבודדות לוקאלית (JSON), בלי ענן
- `app/ui/` - הממשק: סרגל צד עם עץ (NVR -> ערוצים, ומצלמות בודדות) + גריד תצוגה
- `build_exe.bat` - אריזה ל-EXE בודד עם PyInstaller (להריץ על Windows)
- `uninstall.bat` - הסרה נקייה (מוחק את קובץ ה-JSON השמור ומציע למחוק את ה-EXE)

## הרצה בפיתוח
```
python -m venv .venv
.venv\Scripts\activate      (Windows)   /   source .venv/bin/activate (Linux/Mac)
pip install -r requirements.txt
python -m app.main
```

## בניית EXE
הרץ `build_exe.bat` בתוך סביבת venv עם התלויות מותקנות. הקובץ הסופי יופיע ב-`dist\UniversalCamViewer.exe`.

## העלאה ל-GitHub
**אין להדביק PAT token בשום מקום בקוד או בצ'אט.** במקום זה, מהמחשב המקומי:
```
cd camera-viewer
git init
git add .
git commit -m "Initial commit - universal camera viewer skeleton"
git branch -M main
git remote add origin https://github.com/koby479/camera-.git
git push -u origin main
```
כשגיט יבקש התחברות - השתמש ב-Git Credential Manager (חלון login רגיל) או ב-SSH key,
לא בהדבקת טוקן גולמי לתוך שורת פקודה/צ'אט. אם כבר יצרת PAT וחשפת אותו - בטל אותו עכשיו
תחת GitHub -> Settings -> Developer settings -> Personal access tokens, גם אם הריפו ריק.

## הערה לגבי Provision CMS3
לפני שמתקינים תוכנה נוספת - כדאי לבדוק בתוך Provision CMS3 עצמו אם יש אופציה
להוספת "Third-party ONVIF Device / IP Camera" ולחבר את המצלמה הסינית ישירות דרכה
(IP + פורט ONVIF + סיסמה). קושחאות NVR עדכניות של Provision-ISR תומכות ONVIF G/T/S
ויכולות להתחבר ל-VMS צד-שלישי שתומך ONVIF.

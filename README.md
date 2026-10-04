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

## הרצה רגילה (בלי פקודות)
לחיצה כפולה על `start.pyw`. כדי לקבל אייקון בשולחן העבודה: לחיצה כפולה על `create_shortcut.bat` (פעם אחת).
שגיאות נשמרות ב-`%LOCALAPPDATA%\UniversalCamViewer\app.log`.

## הרצה בפיתוח
```
python -m venv .venv
.venv\Scripts\activate      (Windows)   /   source .venv/bin/activate (Linux/Mac)
pip install -r requirements.txt
python -m app.main
```

## מכשירי XM / Provision CMS (פורט 34567)
ב"הוסף NVR" בחר "סוג חיבור: XM / Provision CMS" והזן את אותם IP, פורט (34567), משתמש וסיסמה כמו ב-CMS3.
נדרש `av` (מותקן דרך requirements.txt) לפענוח H.264.
לאבחון: `python tools/diagnose_nvr.py IP --user X --password Y --stream-test`

## חיבור מחדש
קליק ימני על אריח: **התחבר מחדש** (אותה מצלמה, אותו מקום ברשת), **התחבר מחדש בזרם ראשי/משני**,
ו-**שנה פרטי חיבור** (כתובת, פורט, סיסמה). אחרי שינוי פרטים של NVR כל הערוצים שלו מתחברים מחדש עם הפרטים החדשים.
אחרי 3 ניסיונות עם סיסמה שגויה המצלמה מפסיקה לנסות (כדי לא לנעול את המשתמש ב-NVR). תיקון הפרטים מחבר אותה שוב.

## נגן הקלטות
בחלון ההקלטות: **▶ נגן במלא** פותח נגן מלא ומתחיל לנגן מיד. ההקלטה יורדת ברקע לקובץ זמני
(`%LOCALAPPDATA%\\UniversalCamViewer\\cache`, נמחק אוטומטית כשהוא עובר 12GB). אפשר לקפוץ אחורה ולנגן לאחור בכל רגע,
וקדימה עד המקום שכבר ירד (הפס הדק מתחת לסרגל). כשההקלטה ירדה כולה, **💾 שמור כ-MP4** שומר אותה. פתיחה חוזרת של אותה
הקלטה מיידית. **📂 פתח קובץ בנגן** פותח קובץ וידאו שכבר יש במחשב.
בנגן: סרגל קפיצה, ניגון לאחור, פריים-פריים, מהירויות 0.25x עד 16x, לולאה בין שתי נקודות (A/B), זום, צילום ומסך מלא.
קיצורים: רווח, חיצים, J/L, פסיק/נקודה, [ ], R, A/B/C, S, F. כשיש תקלה בקובץ: `python tools/diagnose_player.py קובץ.mp4`.

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

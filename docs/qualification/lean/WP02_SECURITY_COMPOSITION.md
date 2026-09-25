# WP-02 — безпечна відтворювана композиція LEAN

База AutoTrade: `fb6132d9d1bc145b7d995296859b33ac57888803`.

Затверджене вихідне джерело LEAN не перепозначається і не підмінюється: workflow спочатку перевіряє exact commit, tree та license blob з `provenance/components.json`, а лише потім створює окрему AutoTrade composition.

Причина:
- upstream `Compression/QuantConnect.Compression.csproj` містить `DotNetZip 1.16.0`, для якого немає виправленої версії пакета;
- transitive graph містить `System.Drawing.Common 4.7.0`;
- NuGet audit правильно блокує ці версії, і його не можна вимикати або приховувати через `NoWarn`.

Композиція v1:
- `DotNetZip 1.16.0` замінюється на `ProDotNetZip 1.20.0`; простір імен `Ionic.Zip` зберігається, а форк містить виправлення CVE-2024-48510;
- у `Common/QuantConnect.csproj` явно фіксується `System.Drawing.Common 4.7.2`, тобто перша виправлена лінія для GHSA-rxg9-xrhp-64gj;
- дозволено змінити рівно два LEAN-файли;
- перед заміною кожен anchor мусить зустрічатися рівно один раз; невідома upstream-форма fail-closed;
- для кожного файла зберігаються SHA-256 до/після, а canonical composition отримує власний SHA-256;
- workflow після probe повторно перевіряє, що LEAN tree має лише ці дві композиційні зміни;
- exact project.assets manifests хешуються як evidence.

Ця зміна не надає trading authority, не доводить economic edge і не означає повне завершення WP-02/WP-03/WP-64. PASS можливий лише після двоплатформного exact-head build/probe із незмінно увімкненим NuGet audit.

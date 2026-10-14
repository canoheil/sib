# Phase 1 — proposed region seeds (SQL reference)

**Documentation only.** The `regions` table does not exist for this design yet; Phase 2 owns table creation and any seed operation. Do not run this SQL during Phase 1. The same exact seed values are included in [PHASE-1-DESIGN.md](PHASE-1-DESIGN.md#exact-seed-data-reference-only-do-not-execute-until-phase-2).

The proposed columns are `code`, `name_fa`, and `controlling_faction`. The literal `بی‌طرف` is preserved for the two neutral regions; Phase 2 must decide whether it is represented as a faction reference or display value before applying the statement. Use a UTF-8-capable connection when eventually executing it.

```sql
INSERT INTO regions (code, name_fa, controlling_faction) VALUES
  ('moonfall_valley', 'درّه ماه', 'فرزندان ماه'),
  ('emerald_forest', 'جنگل زمرد', 'فرزانگان'),
  ('sunfield_plains', 'دشت آفتاب', 'امپراتوری'),
  ('diamond_peaks', 'کوهستان الماس', 'امپراتوری'),
  ('lotus_lake', 'دریاچه نیلوفر', 'فرزندان ماه'),
  ('ash_desert', 'بیابان خاکستر', 'بی‌طرف'),
  ('mirror_city', 'شهر آینه', 'امپراتوری'),
  ('whispering_caves', 'غار زمزمه‌ها', 'بی‌طرف'),
  ('star_harbor', 'بندر ستاره', 'امپراتوری'),
  ('dark_marsh', 'تالاب تاریک', 'فرزندان ماه');
```

Exactly **10** rows; no duplicate keys, no schema or migration in this file. The controlling names, including the neutral value, are kept exactly as supplied.

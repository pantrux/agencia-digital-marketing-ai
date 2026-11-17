ALTER TABLE production_briefs ADD COLUMN ad_cta TEXT NOT NULL DEFAULT ''
    CHECK (length(ad_cta) <= 70 AND instr(ad_cta, char(10)) = 0 AND instr(ad_cta, char(13)) = 0);
ALTER TABLE production_briefs ADD COLUMN ad_brand TEXT NOT NULL DEFAULT ''
    CHECK (length(ad_brand) <= 80 AND (ad_cta = '') = (ad_brand = '')
        AND instr(ad_brand, char(10)) = 0 AND instr(ad_brand, char(13)) = 0);

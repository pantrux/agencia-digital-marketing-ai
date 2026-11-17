ALTER TABLE production_briefs ADD COLUMN ad_cta TEXT NOT NULL DEFAULT ''
    CHECK (length(ad_cta) <= 70 AND instr(ad_cta, char(10)) = 0 AND instr(ad_cta, char(13)) = 0);

ALTER TABLE production_briefs ADD COLUMN ad_engine TEXT NOT NULL DEFAULT ''
    CHECK (ad_engine IN ('', 'hyperframes') AND (ad_engine = '' OR ad_cta <> ''));

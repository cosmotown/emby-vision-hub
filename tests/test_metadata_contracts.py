import unittest

from metadata_contracts import (
    structured_from_canonical,
    structured_to_canonical,
)


class MetadataContractTests(unittest.TestCase):
    def test_movie_ratings_round_trip_between_db_and_release_dates(self):
        database = {"US": "PG-13", "KR": "15"}
        shenyi = structured_from_canonical(
            "official_rating_json", "Movie", database, "shenyi_override"
        )

        self.assertEqual(
            database,
            structured_to_canonical(
                "official_rating_json",
                "Movie",
                shenyi,
                "shenyi_override",
            ),
        )
        self.assertIsInstance(shenyi["results"], list)
        self.assertIsInstance(shenyi["results"][0]["release_dates"], list)

    def test_series_ratings_round_trip_between_db_and_content_ratings(self):
        database = {"US": "TV-MA", "CN": "16+"}
        shenyi = structured_from_canonical(
            "official_rating_json", "Series", database, "shenyi_override"
        )

        self.assertEqual(
            database,
            structured_to_canonical(
                "official_rating_json",
                "Series",
                shenyi,
                "shenyi_tmdb_cache",
            ),
        )
        self.assertEqual(
            {"descriptors", "iso_3166_1", "rating"},
            set(shenyi["results"][0]),
        )

    def test_movie_country_codes_round_trip_to_production_countries(self):
        database = ["KR", "US"]
        shenyi = structured_from_canonical(
            "countries_json", "Movie", database, "shenyi_override"
        )

        self.assertEqual(
            database,
            structured_to_canonical(
                "countries_json", "Movie", shenyi, "shenyi_tmdb_cache"
            ),
        )
        self.assertEqual(
            [
                {"iso_3166_1": "KR", "name": "KR"},
                {"iso_3166_1": "US", "name": "US"},
            ],
            shenyi,
        )

    def test_series_country_codes_keep_origin_country_schema(self):
        database = ["KR", "US"]
        shenyi = structured_from_canonical(
            "countries_json", "Series", database, "shenyi_override"
        )
        self.assertEqual(database, shenyi)
        self.assertEqual(
            database,
            structured_to_canonical(
                "countries_json", "Series", shenyi, "shenyi_tmdb_cache"
            ),
        )

    def test_schema_validation_rejects_cross_schema_values(self):
        with self.assertRaisesRegex(ValueError, "release_dates"):
            structured_to_canonical(
                "official_rating_json",
                "Movie",
                {"results": [{"iso_3166_1": "US", "rating": "PG"}]},
                "shenyi_override",
            )
        with self.assertRaisesRegex(ValueError, "production_countries"):
            structured_to_canonical(
                "countries_json",
                "Movie",
                ["US"],
                "shenyi_override",
            )
        with self.assertRaisesRegex(ValueError, "国家代码"):
            structured_to_canonical(
                "official_rating_json",
                "Series",
                {"results": []},
                "evh_database",
            )

    def test_other_structured_fields_use_validated_object_list_contracts(self):
        values = [{"id": 18, "name": "Drama"}]
        for field in (
            "genres_json",
            "production_companies_json",
            "networks_json",
        ):
            with self.subTest(field=field):
                canonical = structured_to_canonical(
                    field, "Series", values, "shenyi_tmdb_cache"
                )
                self.assertEqual(values, canonical)
                self.assertEqual(
                    values,
                    structured_from_canonical(
                        field, "Series", canonical, "evh_database"
                    ),
                )
                with self.assertRaisesRegex(ValueError, "对象"):
                    structured_to_canonical(
                        field, "Series", ["invalid"], "evh_database"
                    )


if __name__ == "__main__":
    unittest.main()

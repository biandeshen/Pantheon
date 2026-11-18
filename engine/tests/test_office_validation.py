"""Tests for server-side office design validation in orgs.update_org.

Mirrors the frontend's isDesign() check so malformed layouts are caught at the
API rather than silently persisted and lost on reload.
"""
import pytest
from app.services.orgs import _validate_office_design


class TestValidateOfficeDesign:
    def test_accepts_valid_design(self):
        design = {"version": 1, "items": [], "rooms": [], "width": 20, "depth": 20}
        assert _validate_office_design(design) is True

    def test_accepts_design_with_items_and_rooms(self):
        design = {
            "version": 1,
            "items": [{"id": "it_1", "kind": "workstation", "x": 1, "z": 1, "rot": 0}],
            "rooms": [{"id": "rm_1", "name": "Lounge", "type": "lounge", "x": 0, "z": 0, "w": 5, "d": 5, "walls": "glass", "door": "n"}],
            "width": 20,
            "depth": 20,
        }
        assert _validate_office_design(design) is True

    def test_rejects_none(self):
        assert _validate_office_design(None) is False

    def test_rejects_empty_dict(self):
        assert _validate_office_design({}) is False

    def test_rejects_wrong_version(self):
        design = {"version": 2, "items": [], "rooms": [], "width": 10, "depth": 10}
        assert _validate_office_design(design) is False

    def test_rejects_non_array_items(self):
        design = {"version": 1, "items": "not-array", "rooms": [], "width": 10, "depth": 10}
        assert _validate_office_design(design) is False

    def test_rejects_non_array_rooms(self):
        design = {"version": 1, "items": [], "rooms": "not-array", "width": 10, "depth": 10}
        assert _validate_office_design(design) is False

    def test_rejects_zero_width(self):
        design = {"version": 1, "items": [], "rooms": [], "width": 0, "depth": 10}
        assert _validate_office_design(design) is False

    def test_rejects_zero_depth(self):
        design = {"version": 1, "items": [], "rooms": [], "width": 10, "depth": 0}
        assert _validate_office_design(design) is False

    def test_rejects_negative_width(self):
        design = {"version": 1, "items": [], "rooms": [], "width": -5, "depth": 10}
        assert _validate_office_design(design) is False

    def test_rejects_string_width(self):
        design = {"version": 1, "items": [], "rooms": [], "width": "ten", "depth": 10}
        assert _validate_office_design(design) is False

    def test_rejects_non_dict(self):
        assert _validate_office_design("not-a-dict") is False
        assert _validate_office_design(42) is False
        assert _validate_office_design([]) is False

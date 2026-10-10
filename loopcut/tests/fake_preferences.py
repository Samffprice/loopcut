"""Reject mapping storage on preference fakes, as Blender's non-ID RNA struct does."""


class AddonPreferences:
    def get(self, *args):
        raise TypeError("this type doesn't support IDProperties")

    def __getitem__(self, key):
        raise TypeError("this type doesn't support IDProperties")

    def __setitem__(self, key, value):
        raise TypeError("this type doesn't support IDProperties")

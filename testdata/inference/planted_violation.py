"""Synthetic module. The boundary scan rejects this model call."""


def classify(model):
    return model.predict("synthetic thread")

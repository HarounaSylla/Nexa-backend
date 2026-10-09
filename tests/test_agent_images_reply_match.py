import uuid

from app.agent.images import (
    ProductImageRef,
    images_named_in_reply,
    normalize_for_reply_match,
    reply_names_product,
)


def test_normalize_casefold_accents_and_punctuation() -> None:
    assert normalize_for_reply_match("Montre femme or rose") == (
        "montre femme or rose"
    )
    assert normalize_for_reply_match("montre femme or rose") == (
        "montre femme or rose"
    )
    assert normalize_for_reply_match("Robe") == "robe"
    assert normalize_for_reply_match("  Robe,   élégante!!  ") == "robe elegante"


def test_reply_names_product_case_and_accents() -> None:
    reply = "Oui, la montre femme or rose à 9 500 F."
    assert reply_names_product(reply, "Montre femme or rose")
    assert reply_names_product(reply, "montre femme or rose")
    assert reply_names_product("Voici une Robe", "robe")
    assert not reply_names_product(reply, "Robe de soirée rouge")


def test_reply_names_product_empty_name_is_not_named() -> None:
    assert not reply_names_product("bonjour", "")
    assert not reply_names_product("bonjour", "   ")


def test_substring_product_names_shorter_reply_matches_only_short_name() -> None:
    """A shorter product name is not enough to match a longer product name.

    The reverse is true: a reply that contains the longer name also contains
    the shorter name as a substring, so both products count as named.
    """
    short = "Montre"
    long_name = "Montre femme or rose et noir"
    assert reply_names_product("Je prends la montre.", short)
    assert not reply_names_product("Je prends la montre.", long_name)
    assert reply_names_product(
        "Je prends la montre femme or rose et noir.",
        short,
    )
    assert reply_names_product(
        "Je prends la montre femme or rose et noir.",
        long_name,
    )


def _refs(*product_ids: uuid.UUID) -> list[ProductImageRef]:
    return [
        ProductImageRef(product_id=product_id, image_url="x.jpg")
        for product_id in product_ids
    ]


def test_images_named_in_reply_keeps_tool_output_order() -> None:
    first = uuid.uuid4()
    second = uuid.uuid4()
    names = {first: "Montre femme or rose", second: "Robe de soirée rouge"}
    images = _refs(first, second)
    reply = (
        "La robe de soirée rouge et la montre femme or rose sont dispo."
    )
    kept = images_named_in_reply(images, names, reply)
    assert [image.product_id for image in kept] == [first, second]


def test_images_named_in_reply_keeps_only_named_product() -> None:
    watch = uuid.uuid4()
    dress = uuid.uuid4()
    names = {watch: "Montre femme or rose", dress: "Robe de soirée rouge"}
    kept = images_named_in_reply(
        _refs(watch, dress),
        names,
        "Oui, la montre femme or rose à 9 500 F.",
    )
    assert [image.product_id for image in kept] == [watch]


def test_images_named_in_reply_empty_when_none_named() -> None:
    watch = uuid.uuid4()
    dress = uuid.uuid4()
    names = {watch: "Montre femme or rose", dress: "Robe de soirée rouge"}
    kept = images_named_in_reply(
        _refs(watch, dress),
        names,
        "Voici quelques articles.",
    )
    assert kept == []

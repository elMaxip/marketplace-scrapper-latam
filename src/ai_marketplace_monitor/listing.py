from dataclasses import asdict, dataclass
from typing import ClassVar, Optional, Tuple, Type

from diskcache import Cache  # type: ignore

from .utils import CacheType, cache, hash_dict


@dataclass
class Listing:
    marketplace: str
    name: str
    # unique identification
    id: str
    title: str
    image: str
    price: str
    post_url: str
    location: str
    seller: str
    condition: str
    description: str
    #: How many the shop says it can sell right now, as text, or "".
    #:
    #: Only a retailer has this.  A Facebook or Mercado Libre listing is one
    #: object with one seller, and the question does not arise; Lider and
    #: Sodimac publish a number, and it is the number a "stock mínimo" alert is
    #: measured against.  Text and not an int because what the two sites publish
    #: is not the same quantity -- Sodimac's is what it will let you put in a
    #: cart, Lider's is a per-order ceiling -- and rounding both into one
    #: integer would invent a precision neither of them offers.
    stock: str = ""
    #: ``"in_stock"``, ``"out_of_stock"`` or "" when the site does not say.
    #:
    #: Kept apart from ``stock`` because they answer different questions and a
    #: site can answer one without the other: "no stock number, but you can buy
    #: it" is the ordinary state of a marketplace listing.
    availability: str = ""

    #: Fields that are not part of "has this listing changed?".
    #:
    #: ``image`` because Facebook's URLs carry an expiring signature, so every
    #: scrape would report a change.  ``stock`` and ``availability`` because a
    #: shop's counter ticking from 45 to 44 is not news, and telling the user
    #: their listing "changed" every time somebody else buys one is how a
    #: notification channel gets muted.  Availability going to zero is not lost:
    #: it comes back as :attr:`~ai_marketplace_monitor.marketplace.ListingStatus.SOLD`
    #: from the re-check, which removes the listing outright.
    NOT_IN_HASH: ClassVar[Tuple[str, ...]] = ("image", "stock", "availability")

    @property
    def content(self: "Listing") -> Tuple[str, str, str]:
        return (self.title, self.description, self.price)

    @property
    def hash(self: "Listing") -> str:
        # we need to normalize post_url before hashing because post_url will be different
        # each time from a search page. We also does not count image
        return hash_dict(
            {
                x: (y.split("?")[0] if x == "post_url" else y)
                for x, y in asdict(self).items()
                if x not in self.NOT_IN_HASH
            }
        )

    @classmethod
    def from_cache(
        cls: Type["Listing"],
        post_url: str,
        local_cache: Cache | None = None,
    ) -> Optional["Listing"]:
        try:
            # details could be a different datatype, miss some key etc.
            # and we have recently changed to save Listing as a dictionary
            return cls(
                **(cache if local_cache is None else local_cache).get(
                    (CacheType.LISTING_DETAILS.value, post_url.split("?")[0])
                )
            )
        except KeyboardInterrupt:
            raise
        except Exception:
            return None

    def to_cache(
        self: "Listing",
        post_url: str,
        local_cache: Cache | None = None,
    ) -> None:
        (cache if local_cache is None else local_cache).set(
            (CacheType.LISTING_DETAILS.value, post_url.split("?")[0]),
            asdict(self),
            tag=CacheType.LISTING_DETAILS.value,
        )

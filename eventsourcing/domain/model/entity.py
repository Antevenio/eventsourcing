from decimal import Decimal
from typing import Callable, Optional, cast, Type, Dict, Any
from uuid import uuid4, UUID

import eventsourcing.domain.model.events as events
from eventsourcing.domain.model.decorators import subclassevents
from eventsourcing.domain.model.events import (
    DomainEvent,
    EventWithHash,
    EventWithOriginatorID,
    EventWithOriginatorVersion,
    EventWithTimestamp,
    GENESIS_HASH,
    publish,
)
from eventsourcing.exceptions import (
    EntityIsDiscarded,
    HeadHashError,
    OriginatorIDError,
    OriginatorVersionError,
)
from eventsourcing.types import M, MetaAbstractDomainEntity, N
from eventsourcing.utils.times import decimaltimestamp_from_uuid
from eventsourcing.utils.topic import get_topic, resolve_topic


class MetaDomainEntity(MetaAbstractDomainEntity):
    __subclassevents__ = False

    def __init__(cls, name, bases, attrs):
        super().__init__(name, bases, attrs)
        if cls.__subclassevents__:
            subclassevents(cls)


class DomainEntity(metaclass=MetaDomainEntity):
    """
    Supertype for domain model entity.
    """

    __subclassevents__ = False

    class Event(EventWithOriginatorID, DomainEvent):
        """
        Supertype for events of domain model entities.
        """

        def __check_obj__(self, obj: "DomainEntity") -> None:
            """
            Checks state of obj before mutating.

            :param obj: Domain entity to be checked.

            :raises OriginatorIDError: if the originator_id is mismatched
            """
            # Check obj is not None.
            assert obj is not None, "'obj' is None"

            # Check ID matches originator ID.
            obj = cast(DomainEntity, obj)
            if obj.id != self.originator_id:
                raise OriginatorIDError(
                    "'{}' not equal to event originator ID '{}'"
                    "".format(obj.id, self.originator_id)
                )

    @classmethod
    def __create__(
        cls, originator_id: UUID = None, event_class: M = None, **kwargs
    ) -> N:
        """
        Creates a new domain entity.

        Constructs a "created" event, constructs the entity object
        from the event, publishes the "created" event, and returns
        the new domain entity object.

        :param originator_id: ID of the new domain entity (defaults to ``uuid4()``).
        :param event_class: Domain event class to be used for the "created" event.
        :param kwargs: Other named attribute values of the "created" event.
        :return: New domain entity object.
        :rtype: DomainEntity
        """
        if originator_id is None:
            originator_id = uuid4()
        event = (event_class or cls.Created)(
            originator_id=originator_id, originator_topic=get_topic(cls), **kwargs
        )
        obj = event.__mutate__(None)
        assert obj is not None, "{} returned None".format(
            type(event).__mutate__.__qualname__
        )
        obj.__publish__(event)
        return obj

    class Created(Event, events.Created):
        """
        Triggered when an entity is created.
        """

        def __init__(self, originator_topic, **kwargs):
            super(DomainEntity.Created, self).__init__(
                originator_topic=originator_topic, **kwargs
            )

        @property
        def originator_topic(self) -> str:
            """
            Topic (a string) representing the class of the originating domain entity.

            :rtype: str
            """
            return self.__dict__["originator_topic"]

        def __mutate__(self, obj: Optional[N] = None) -> Optional[N]:
            """
            Constructs object from an entity class,
            which is obtained by resolving the originator topic,
            unless it is given as method argument ``entity_class``.

            :param entity_class: Class of domain entity to be constructed.
            """
            entity_class: Callable
            if obj is None:
                entity_class = resolve_topic(self.originator_topic)
            else:
                entity_class = cast(MetaDomainEntity, obj)
            return entity_class(**self.__entity_kwargs__)

        @property
        def __entity_kwargs__(self) -> Dict[str, Any]:
            kwargs = self.__dict__.copy()
            kwargs["id"] = kwargs.pop("originator_id")
            kwargs.pop("originator_topic", None)
            kwargs.pop("__event_topic__", None)
            return kwargs

    def __init__(self, id):
        self._id = id
        self.__is_discarded__ = False

    @property
    def id(self) -> UUID:
        """The immutable ID of the domain entity.

        This value is set using the ``originator_id`` of the
        "created" event constructed by ``__create__()``.

        An entity ID allows an instance to be
        referenced and distinguished from others, even
        though its state may change over time.

        This attribute has the normal "public" format for a Python object
        attribute name, because by definition all domain entities have an ID.
        """
        return self._id

    def __change_attribute__(self, name: str, value: Any) -> None:
        """
        Changes named attribute with the given value,
        by triggering an AttributeChanged event.
        """
        self.__trigger_event__(self.AttributeChanged, name=name, value=value)

    class AttributeChanged(Event, events.AttributeChanged):
        """
        Triggered when a named attribute is assigned a new value.
        """

        def __mutate__(self, obj: Optional[N] = None) -> Optional[N]:
            obj = super(DomainEntity.AttributeChanged, self).__mutate__(obj)
            setattr(obj, self.name, self.value)
            return obj

    def __discard__(self):
        """
        Discards self, by triggering a Discarded event.
        """
        self.__trigger_event__(self.Discarded)

    class Discarded(events.Discarded, Event):
        """
        Triggered when a DomainEntity is discarded.
        """

        def __mutate__(self, obj: Optional[N] = None) -> Optional[N]:
            obj = super(DomainEntity.Discarded, self).__mutate__(obj)
            entity = cast(DomainEntity, obj)
            entity.__is_discarded__ = True
            return None

    def __assert_not_discarded__(self):
        """
        Raises exception if entity has been discarded already.
        """
        if self.__is_discarded__:
            raise EntityIsDiscarded("Entity is discarded")

    def __trigger_event__(self, event_class: Type[DomainEvent], **kwargs) -> None:
        """
        Constructs, applies, and publishes a domain event.
        """
        self.__assert_not_discarded__()
        event = event_class(originator_id=self._id, **kwargs)
        self.__mutate__(event)
        self.__publish__(event)

    def __mutate__(self, event):
        """
        Mutates this entity with the given event.

        This method calls on the event object to mutate this
        entity, because the mutation behaviour of different types
        of events was nicely factored onto the event classes, and
        the event mutate() method is the most convenient way to
        defined behaviour in domain models.

        However, as an alternative to implementing the mutate()
        method on domain model events, this method can be extended
        in domain model entities by implementing a mutator function
        that is capable of mutating this entity for all its domain event
        types.

        Similarly, this method can be overridden entirely in subclasses,
        so long as all entity mutation behaviour is implemented in the
        mutator function, including the mutation behaviour of the different
        types of event defined in the library that would otherwise be invoked.
        """
        event.__mutate__(self)

    def __publish__(self, event):
        """
        Publishes given event for subscribers in the application.

        :param event: domain event or list of events
        """
        self.__publish_to_subscribers__(event)

    def __publish_to_subscribers__(self, event):
        """
        Actually dispatches given event to publish-subscribe mechanism.

        :param event: domain event or list of events
        """
        publish(event)

    def __eq__(self, other):
        return type(self) == type(other) and self.__dict__ == other.__dict__

    def __ne__(self, other):
        return not self.__eq__(other)


class EntityWithHashchain(DomainEntity):
    __genesis_hash__ = GENESIS_HASH

    def __init__(self, *args, **kwargs):
        super(EntityWithHashchain, self).__init__(*args, **kwargs)
        self.__head__ : str = type(self).__genesis_hash__

    class Event(EventWithHash, DomainEntity.Event):
        """
        Supertype for events of domain entities.
        """

        def __mutate__(self, obj: Optional[N] = None) -> Optional[N]:
            # Call super method.
            obj = super(EntityWithHashchain.Event, self).__mutate__(obj)

            # Set entity head from event hash.
            #  - unless just discarded...
            if obj is not None:
                entity_with_hashchain = cast(EntityWithHashchain, obj)
                entity_with_hashchain.__head__ = self.__event_hash__

            return obj

        def __check_obj__(self, obj: DomainEntity):
            """
            Extends superclass method by checking the __previous_hash__
            of this event matches the __head__ hash of the entity obj.
            """
            # Call super method.
            super(EntityWithHashchain.Event, self).__check_obj__(obj)

            # Check __head__ matches previous hash.
            obj = cast(EntityWithHashchain, obj)

            if obj.__head__ != self.__dict__.get("__previous_hash__"):
                raise HeadHashError(obj.id, obj.__head__, type(self))

    class Created(Event, DomainEntity.Created):
        @property
        def __entity_kwargs__(self) -> Dict[str, Any]:
            # Get super property.
            kwargs = super(EntityWithHashchain.Created, self).__entity_kwargs__

            # Drop the event hashes.
            kwargs.pop("__event_hash__", None)
            kwargs.pop("__previous_hash__", None)

            return kwargs

        def __mutate__(self, obj: Optional[N] = None) -> Optional[N]:
            # Call super method.
            return super(EntityWithHashchain.Created, self).__mutate__(obj)

    class AttributeChanged(Event, DomainEntity.AttributeChanged):
        pass

    class Discarded(Event, DomainEntity.Discarded):
        def __mutate__(self, obj: Optional[N] = None) -> Optional[N]:
            # Set entity head from event hash.
            entity = cast(EntityWithHashchain, obj)
            entity.__head__ = self.__event_hash__

            # Call super method.
            return super(EntityWithHashchain.Discarded, self).__mutate__(obj)

    @classmethod
    def __create__(cls, *args, **kwargs) -> N:
        kwargs["__previous_hash__"] = getattr(cls, "__genesis_hash__", GENESIS_HASH)
        return super(EntityWithHashchain, cls).__create__(*args, **kwargs)

    def __trigger_event__(self, event_class: Type[DomainEvent], **kwargs) -> None:
        assert isinstance(event_class, type), type(event_class)
        kwargs["__previous_hash__"] = self.__head__
        super(EntityWithHashchain, self).__trigger_event__(event_class, **kwargs)


class VersionedEntity(DomainEntity):
    def __init__(self, __version__: int, **kwargs):
        super().__init__(**kwargs)
        self.___version__: int = __version__

    @property
    def __version__(self) -> int:
        return self.___version__

    def __trigger_event__(self, event_class: Type[DomainEvent], **kwargs) -> None:
        """
        Triggers domain event with entity's next version number.

        The event carries the version number that the originator
        will have when the originator is mutated with this event.
        (The event's originator version isn't the version of the
        originator that triggered the event. The Created event has
        version 0, and so a newly created instance is at version 0.
        The second event has originator version 1, and so will the
        originator when the second event has been applied.)
        """
        return super(VersionedEntity, self).__trigger_event__(
            event_class=event_class, originator_version=self.__version__ + 1, **kwargs
        )

    class Event(EventWithOriginatorVersion, DomainEntity.Event):
        """Supertype for events of versioned entities."""

        def __mutate__(self, obj: Optional[N] = None) -> Optional[N]:
            obj = super(VersionedEntity.Event, self).__mutate__(obj)
            if obj is not None:
                entity = cast(EventWithOriginatorVersion, obj)
                entity.___version__ = self.originator_version
            return obj

        def __check_obj__(self, obj: "DomainEntity") -> None:
            """
            Extends superclass method by checking the event's
            originator version follows (1 +) this entity's version.
            """
            super(VersionedEntity.Event, self).__check_obj__(obj)
            obj = cast(VersionedEntity, obj)
            if self.originator_version != obj.__version__ + 1:
                raise OriginatorVersionError(
                    (
                        "Event takes entity to version {}, "
                        "but entity is currently at version {}. "
                        "Event type: '{}', entity type: '{}', entity ID: '{}'"
                        "".format(
                            self.originator_version,
                            obj.__version__,
                            type(self).__name__,
                            type(obj).__name__,
                            obj._id,
                        )
                    )
                )

    class Created(DomainEntity.Created, Event):
        """Published when a VersionedEntity is created."""

        def __init__(self, originator_version=0, **kwargs):
            super(VersionedEntity.Created, self).__init__(
                originator_version=originator_version, **kwargs
            )

        @property
        def __entity_kwargs__(self) -> Dict[str, Any]:
            # Get super property.
            kwargs = super(VersionedEntity.Created, self).__entity_kwargs__
            kwargs["__version__"] = kwargs.pop("originator_version")
            return kwargs

    class AttributeChanged(Event, DomainEntity.AttributeChanged):
        """Published when a VersionedEntity is changed."""

    class Discarded(Event, DomainEntity.Discarded):
        """Published when a VersionedEntity is discarded."""


class TimestampedEntity(DomainEntity):
    def __init__(self, __created_on__: Decimal, **kwargs):
        super(TimestampedEntity, self).__init__(**kwargs)
        self.___created_on__ = __created_on__
        self.___last_modified__ = __created_on__

    @property
    def __created_on__(self) -> Decimal:
        return self.___created_on__

    @property
    def __last_modified__(self) -> Decimal:
        return self.___last_modified__

    class Event(DomainEntity.Event, EventWithTimestamp):
        """Supertype for events of timestamped entities."""

        def __mutate__(self, obj: Optional[N] = None) -> Optional[N]:
            """Updates 'obj' with values from self."""
            obj = super(TimestampedEntity.Event, self).__mutate__(obj)
            if obj is not None:
                assert isinstance(obj, TimestampedEntity), obj
                obj.___last_modified__ = self.timestamp
            return obj

    class Created(DomainEntity.Created, Event):
        """Published when a TimestampedEntity is created."""

        @property
        def __entity_kwargs__(self) -> Dict[str, Any]:
            # Get super property.
            kwargs = super(TimestampedEntity.Created, self).__entity_kwargs__
            kwargs["__created_on__"] = kwargs.pop("timestamp")
            return kwargs

    class AttributeChanged(Event, DomainEntity.AttributeChanged):
        """Published when a TimestampedEntity is changed."""

    class Discarded(Event, DomainEntity.Discarded):
        """Published when a TimestampedEntity is discarded."""


# Todo: Move stuff from "test_customise_with_alternative_domain_event_type" in here (
#  to define event classes
#  and update ___last_event_id__ in mutate method).

class TimeuuidedEntity(DomainEntity):
    def __init__(self, event_id, **kwargs):
        super(TimeuuidedEntity, self).__init__(**kwargs)
        self.___initial_event_id__ = event_id
        self.___last_event_id__ = event_id

    @property
    def __created_on__(self) -> Decimal:
        return decimaltimestamp_from_uuid(self.___initial_event_id__)

    @property
    def __last_modified__(self) -> Decimal:
        return decimaltimestamp_from_uuid(self.___last_event_id__)


class TimestampedVersionedEntity(TimestampedEntity, VersionedEntity):
    class Event(TimestampedEntity.Event, VersionedEntity.Event):
        """Supertype for events of timestamped, versioned entities."""

    class Created(TimestampedEntity.Created, VersionedEntity.Created, Event):
        """Published when a TimestampedVersionedEntity is created."""

    class AttributeChanged(
        Event, TimestampedEntity.AttributeChanged, VersionedEntity.AttributeChanged
    ):
        """Published when a TimestampedVersionedEntity is created."""

    class Discarded(Event, TimestampedEntity.Discarded, VersionedEntity.Discarded):
        """Published when a TimestampedVersionedEntity is discarded."""


class TimeuuidedVersionedEntity(TimeuuidedEntity, VersionedEntity):
    pass

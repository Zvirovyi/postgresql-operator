# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Logical Replication implementation.

TODO: add description after specification is accepted.
"""

import json
import logging

from ops import (
    BlockedStatus,
    EventBase,
    LeaderElectedEvent,
    Object,
    RelationBrokenEvent,
    RelationChangedEvent,
    RelationDepartedEvent,
    RelationJoinedEvent,
    Secret,
    SecretChangedEvent,
    SecretNotFoundError,
)

from cluster_topology_observer import ClusterTopologyChangeEvent
from constants import (
    APP_SCOPE,
    PEER,
    USER,
    USER_PASSWORD_KEY,
)

logger = logging.getLogger(__name__)

LOGICAL_REPLICATION_OFFER_RELATION = "logical-replication-offer"
LOGICAL_REPLICATION_RELATION = "logical-replication"
SECRET_LABEL = "logical-replication-secret"  # noqa: S105
LOGICAL_REPLICATION_VALIDATION_ERROR_STATUS = "Logical replication setup is invalid. Check logs"


class PostgreSQLLogicalReplication(Object):
    """Defines the logical-replication logic."""

    def __init__(self, charm):
        super().__init__(charm, "postgresql_logical_replication")
        self.charm = charm
        # Relations
        self.charm.framework.observe(
            self.charm.on[LOGICAL_REPLICATION_OFFER_RELATION].relation_joined,
            self._on_offer_relation_joined,
        )
        self.charm.framework.observe(
            self.charm.on[LOGICAL_REPLICATION_OFFER_RELATION].relation_changed,
            self._on_offer_relation_changed,
        )
        self.charm.framework.observe(
            self.charm.on[LOGICAL_REPLICATION_OFFER_RELATION].relation_departed,
            self._on_offer_relation_departed,
        )
        self.charm.framework.observe(
            self.charm.on[LOGICAL_REPLICATION_OFFER_RELATION].relation_broken,
            self._on_offer_relation_broken,
        )
        self.charm.framework.observe(
            self.charm.on[LOGICAL_REPLICATION_RELATION].relation_joined, self._on_relation_joined
        )
        self.charm.framework.observe(
            self.charm.on[LOGICAL_REPLICATION_RELATION].relation_changed, self._on_relation_changed
        )
        self.charm.framework.observe(
            self.charm.on[LOGICAL_REPLICATION_RELATION].relation_departed,
            self._on_relation_departed,
        )
        self.charm.framework.observe(
            self.charm.on[LOGICAL_REPLICATION_RELATION].relation_broken, self._on_relation_broken
        )
        # Events
        self.charm.framework.observe(
            self.charm.on.cluster_topology_change, self._on_cluster_topology_change
        )
        self.charm.framework.observe(
            self.charm.on.leader_elected, self._on_cluster_topology_change
        )
        self.framework.observe(self.charm.on.secret_changed, self._on_secret_changed)

    # region Relations

    def _on_offer_relation_joined(self, event: RelationJoinedEvent):
        if not self.charm.unit.is_leader():
            return
        if not self.charm.primary_endpoint:
            event.defer()
            logger.debug(
                f"{LOGICAL_REPLICATION_OFFER_RELATION}: joined event deferred as primary is unavailable right now"
            )
            return

        secret = self._get_secret(event.relation.id)
        secret.grant(event.relation)
        event.relation.data[self.model.app].update({
            "secret-id": secret.id,
        })

    def _on_offer_relation_changed(self, event: RelationChangedEvent):
        if not self.charm.unit.is_leader():
            return
        if not self.charm.primary_endpoint:
            event.defer()
            logger.debug(
                f"{LOGICAL_REPLICATION_OFFER_RELATION}: joined event deferred as primary is unavailable right now"
            )
            return

        # TODO: validation
        subscriptions_request = json.loads(
            event.relation.data[event.app].get("subscription-request", "{}")
        )
        publications = json.loads(event.relation.data[self.model.app].get("publications", "{}"))

        for database, tables in subscriptions_request.items():
            if database in publications:
                continue
            # TODO: validation
            publication_name = f"pub_{database}_{event.relation.id}"
            self.charm.postgresql.create_publication(database, publication_name, tables)
            publications[database] = {
                "publication-name": publication_name,
                "replication-slot-name": publication_name,
            }

        for database, publication in publications.copy().items():
            if database in subscriptions_request:
                continue
            self.charm.postgresql.drop_publication(database, publication["publication-name"])
            del publications[database]

        event.relation.data[self.model.app]["publications"] = json.dumps(publications)
        self.charm.update_config()

    def _on_offer_relation_departed(self, event: RelationDepartedEvent):
        if event.departing_unit == self.charm.unit and self.charm._peers is not None:
            self.charm.unit_peer_data.update({"departing": "True"})

    def _on_offer_relation_broken(self, event: RelationBrokenEvent):
        if not self.charm._peers or self.charm.is_unit_departing:
            logger.debug(
                f"{LOGICAL_REPLICATION_OFFER_RELATION}: skipping departing unit in broken event"
            )
            return
        if not self.charm.unit.is_leader():
            return

        if not self.charm.unit_peer_data.get("sobaka"):
            event.defer()
            self.charm.unit_peer_data["sobaka"] = "True"
            return

        publications = json.loads(event.relation.data[self.model.app].get("publications", "{}"))
        logger.warning(f"AAAA: {json.dumps(publications)}")
        for database, publication in publications.items():
            self.charm.postgresql.drop_publication(database, publication["publication-name"])

        self.charm.update_config()

    def _on_relation_joined(self, event: RelationJoinedEvent):
        if not self.charm.unit.is_leader():
            return
        if "logical-replication-validation-ongoing" in self.charm.app_peer_data:
            event.defer()
            logger.debug(
                "Deferring logical replication joined event as validation is still ongoing"
            )
            return
        if "logical-replication-error" in self.charm.app_peer_data:
            logger.debug(
                "Logical replication: subscription request wasn't pushed to the relation as there is validation error"
            )
            return
        event.relation.data[self.model.app]["subscription-request"] = (
            self.charm.config.logical_replication_subscription_request or ""
        )

    def _on_relation_changed(self, event: RelationChangedEvent):
        if not self._relation_changed_checks(event):
            return
        secret_content = self.model.get_secret(
            id=event.relation.data[event.app]["secret-id"]
        ).get_content(refresh=True)

        subscriptions = json.loads(
            self.charm.app_peer_data.get("logical-replication-subscriptions", "{}")
        )
        publications = json.loads(event.relation.data[event.app].get("publications", "{}"))

        for database, publication in publications.items():
            if database in subscriptions:
                continue
            subscription_name = f"sub_{database}"
            self.charm.postgresql.create_subscription(
                subscription_name,
                secret_content["primary"],
                database,
                secret_content["username"],
                secret_content["password"],
                publication["publication-name"],
                publication["replication-slot-name"],
            )
            subscriptions[database] = subscription_name

        for database, subscription in subscriptions.copy().items():
            if database in publications:
                continue
            self.charm.postgresql.drop_subscription(database, subscription)
            del subscriptions[database]

        self.charm.app_peer_data["logical-replication-subscriptions"] = json.dumps(subscriptions)

    def _on_relation_departed(self, event: RelationDepartedEvent):
        if event.departing_unit == self.charm.unit and self.charm._peers is not None:
            self.charm.unit_peer_data.update({"departing": "True"})

    def _on_relation_broken(self, event: RelationBrokenEvent):
        if not self.charm._peers or self.charm.is_unit_departing:
            logger.debug(
                f"{LOGICAL_REPLICATION_RELATION}: skipping departing unit in broken event"
            )
            return
        if not self.charm.unit.is_leader():
            return
        if not self.charm.primary_endpoint:
            logger.debug(
                f"{LOGICAL_REPLICATION_RELATION}: broken event deferred as primary is unavailable right now"
            )
            event.defer()
            return

        subscriptions = json.loads(
            self.charm.app_peer_data.get("logical-replication-subscriptions", "{}")
        )

        for database, subscription in subscriptions.copy().items():
            self.charm.postgresql.drop_subscription(database, subscription)
            del subscriptions[database]

        self.charm.app_peer_data["logical-replication-subscriptions"] = ""

    # endregion

    # region Events

    def _on_cluster_topology_change(self, event: ClusterTopologyChangeEvent | LeaderElectedEvent):
        if not self.charm.unit.is_leader():
            return
        if not len(self.model.relations.get(LOGICAL_REPLICATION_OFFER_RELATION, ())):
            return
        if not self.charm.primary_endpoint:
            event.defer()
            return
        for relation in self.model.relations.get(LOGICAL_REPLICATION_OFFER_RELATION, ()):
            self._get_secret(relation.id)

    def _on_secret_changed(self, event: SecretChangedEvent):
        if not self.charm.unit.is_leader():
            return
        if not self.charm.primary_endpoint:
            event.defer()
            return

        if (
            len(self.model.relations.get(LOGICAL_REPLICATION_OFFER_RELATION, ()))
            and event.secret.label == f"{PEER}.{self.model.app.name}.app"
        ):
            logger.info("Internal secret changed, updating logical replication secret")
            for relation in self.model.relations.get(LOGICAL_REPLICATION_OFFER_RELATION, ()):
                self._get_secret(relation.id)

        if (
            relation := self.model.get_relation(LOGICAL_REPLICATION_RELATION)
        ) and event.secret.label.startswith(SECRET_LABEL):
            logger.info("Logical replication secret changed, updating subscriptions")
            secret_content = self.model.get_secret(
                id=relation.data[relation.app]["secret-id"], label=SECRET_LABEL
            ).get_content(refresh=True)
            subscriptions = json.loads(
                self.charm.app_peer_data.get("logical-replication-subscriptions", "{}")
            )
            for database, subscription in subscriptions.items():
                self.charm.postgresql.update_subscription(
                    database,
                    subscription,
                    secret_content["primary"],
                    secret_content["username"],
                    secret_content["password"],
                )

    # endregion

    def apply_changed_config(self, event: EventBase) -> bool:
        if not self.charm.unit.is_leader():
            return True
        if not self.charm.primary_endpoint:
            self.charm.app_peer_data["logical-replication-validation-ongoing"] = "True"
            event.defer()
            return False
        del self.charm.app_peer_data["logical-replication-validation-ongoing"]
        if self._validate_subscription_request():
            self._apply_updated_subscription_request()
        return True

    def retry_validations(self):
        if not self.charm.unit.is_leader() or not self.charm.primary_endpoint:
            return
        if (
            "logical-replication-validation-ongoing" not in self.charm.app_peer_data
            and "logical-replication-error" in self.charm.app_peer_data
            and self._validate_subscription_request()
        ):
            self._apply_updated_subscription_request()

    def replication_slots(self) -> dict[str, str]:
        return {
            publication["replication-slot-name"]: database
            for relation in self.model.relations.get(LOGICAL_REPLICATION_OFFER_RELATION, ())
            for database, publication in json.loads(
                relation.data[self.model.app].get("publications", "{}")
            ).items()
        }

    def _apply_updated_subscription_request(self):
        if not (relation := self.model.get_relation(LOGICAL_REPLICATION_RELATION)):
            return
        # TODO: cleanup
        relation.data[self.model.app]["subscription-request"] = (
            self.charm.config.logical_replication_subscription_request
        )

    def _validate_subscription_request(self) -> bool:
        try:
            subscription_request_config = json.loads(
                self.charm.config.logical_replication_subscription_request or "{}"
            )
        except json.JSONDecodeError as err:
            self.charm.unit.status = BlockedStatus(
                "Logical replication setup is invalid. Check logs"
            )
            logger.error(f"Logical replication: subscription request config decode error: {err}")
            self.charm.app_peer_data["logical-replication-error"] = (
                "Logical replication setup is invalid. Check logs"
            )
            return False

        relation = self.model.get_relation(LOGICAL_REPLICATION_RELATION)
        subscription_request_relation = (
            json.loads(relation.data[self.model.app].get("subscription-request", "{}"))
            if relation
            else {}
        )

        for database, schematables in subscription_request_config.items():
            if not self.charm.postgresql.database_exists(database):
                self.charm.unit.status = BlockedStatus(
                    "Logical replication setup is invalid. Check logs"
                )
                logger.error(f"Logical replication validation: database {database} doesn't exist")
                self.charm.app_peer_data["logical-replication-error"] = (
                    "Logical replication setup is invalid. Check logs"
                )
                return False
            for schematable in schematables:
                try:
                    schema, table = schematable.split(".")
                except ValueError:
                    self.charm.unit.status = BlockedStatus(
                        "Logical replication setup is invalid. Check logs"
                    )
                    logger.error(
                        f"Logical replication validation: table format isn't right: {schematable}"
                    )
                    self.charm.app_peer_data["logical-replication-error"] = (
                        "Logical replication setup is invalid. Check logs"
                    )
                    return False
                if not self.charm.postgresql.table_exists(database, schema, table):
                    self.charm.unit.status = BlockedStatus(
                        "Logical replication setup is invalid. Check logs"
                    )
                    logger.error(
                        f"Logical replication validation: table {schematable} doesn't exist"
                    )
                    self.charm.app_peer_data["logical-replication-error"] = (
                        "Logical replication setup is invalid. Check logs"
                    )
                    return False
                already_subscribed = (
                    database in subscription_request_relation
                    and table in subscription_request_relation[database]
                )
                if not already_subscribed and not self.charm.postgresql.is_table_empty(
                    database, schema, table
                ):
                    self.charm.unit.status = BlockedStatus(
                        "Logical replication setup is invalid. Check logs"
                    )
                    logger.error(
                        f"Logical replication validation: table {schematable} isn't empty"
                    )
                    self.charm.app_peer_data["logical-replication-error"] = (
                        "Logical replication setup is invalid. Check logs"
                    )
                    return False

        del self.charm.app_peer_data["logical-replication-error"]
        return True

    def _relation_changed_checks(self, event: RelationChangedEvent) -> bool:
        if not self.charm.unit.is_leader():
            return False
        if not self.charm.primary_endpoint:
            logger.debug(
                f"{LOGICAL_REPLICATION_RELATION}: changed event deferred as primary is unavailable right now"
            )
            event.defer()
            return False
        if not event.relation.data[event.app].get("secret-id"):
            logger.warning(
                f"{LOGICAL_REPLICATION_RELATION}: skipping changed event as there is no secret id in the remote application data"
            )
            return False
        return True

    def _get_secret(self, relation_id: int) -> Secret:
        """Returns logical replication secret. Updates, if content changed."""
        secret_label = f"{SECRET_LABEL}-{relation_id}"
        shared_content = {
            "primary": self.charm.primary_endpoint,
            "username": USER,
            "password": self.charm.get_secret(APP_SCOPE, USER_PASSWORD_KEY),
        }
        try:
            # Avoid recreating the secret.
            secret = self.charm.model.get_secret(label=secret_label)
            if not secret.id:
                # Workaround for the secret id not being set with model uuid.
                secret._id = f"secret://{self.model.uuid}/{secret.get_info().id.split(':')[1]}"
            if secret.peek_content() != shared_content:
                logger.info("Updating outdated secret content")
                secret.set_content(shared_content)
            return secret
        except SecretNotFoundError:
            logger.debug("Secret not found, creating a new one")
            pass
        return self.charm.model.app.add_secret(content=shared_content, label=secret_label)

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
    Relation,
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
                f"{LOGICAL_REPLICATION_OFFER_RELATION} join deferred until primary is available"
            )
            return

        secret = self._get_secret(event.relation.id)
        secret.grant(event.relation)
        event.relation.data[self.model.app]["secret-id"] = secret.id

    def _on_offer_relation_changed(self, event: RelationChangedEvent):
        if not self.charm.unit.is_leader():
            return
        if not self.charm.primary_endpoint:
            event.defer()
            logger.debug(
                f"{LOGICAL_REPLICATION_OFFER_RELATION} change deferred until primary is available"
            )
            return
        self._process_offer(event.relation)

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
            subscription_name = self._subscription_name(event.relation.id, database)
            if database in subscriptions:
                self.charm.postgresql.refresh_subscription(database, subscription_name)
                logger.debug(
                    f"Refreshed subscription {subscription_name} in database {database} due to relation change"
                )
            else:
                publication_name = publication["publication-name"]
                self.charm.postgresql.create_subscription(
                    subscription_name,
                    secret_content["primary"],
                    database,
                    secret_content["username"],
                    secret_content["password"],
                    publication_name,
                    publication["replication-slot-name"],
                )
                logger.debug(
                    f"Created new subscription {subscription_name} for publication {publication_name} in database {database}"
                )
                subscriptions[database] = subscription_name

        for database, subscription in subscriptions.copy().items():
            if database in publications:
                continue
            self.charm.postgresql.drop_subscription(database, subscription)
            logger.debug(f"Dropped redundant subscription {subscription} from database {database}")
            del subscriptions[database]

        self.charm.app_peer_data["logical-replication-subscriptions"] = json.dumps(subscriptions)

    def _on_relation_departed(self, event: RelationDepartedEvent):
        if event.departing_unit == self.charm.unit and self.charm._peers is not None:
            self.charm.unit_peer_data.update({"departing": "True"})

    def _on_relation_broken(self, event: RelationBrokenEvent):
        if not self.charm._peers or self.charm.is_unit_departing:
            logger.debug(f"{LOGICAL_REPLICATION_RELATION} break skipped due to departing unit")
            return
        if not self.charm.unit.is_leader():
            return
        if not self.charm.primary_endpoint:
            event.defer()
            logger.debug(
                f"{LOGICAL_REPLICATION_RELATION} break deferred until primary is available"
            )
            return

        subscriptions = json.loads(
            self.charm.app_peer_data.get("logical-replication-subscriptions", "{}")
        )
        for database, subscription in subscriptions.items():
            self.charm.postgresql.drop_subscription(database, subscription)
            logger.debug(
                f"Dropped subscription {subscriptions} from database {database} due to relation break"
            )
        del self.charm.app_peer_data["logical-replication-subscriptions"]

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
        """Validate & apply (relation) logical_replication_subscription_request config parameter."""
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
        """Run recurrent logical replication validation attempt.

        For subscribers - try to validate & apply subscription request.
        For publishers - try to validate & process all the offer relations.
        """
        if not self.charm.unit.is_leader() or not self.charm.primary_endpoint:
            return
        if (
            "logical-replication-validation-ongoing" not in self.charm.app_peer_data
            and "logical-replication-error" in self.charm.app_peer_data
            and self._validate_subscription_request()
        ):
            self._apply_updated_subscription_request()
        for relation in self.model.relations.get(LOGICAL_REPLICATION_OFFER_RELATION, ()):
            if json.loads(relation.data[self.model.app].get("errors", "[]")):
                self._process_offer(relation)

    def replication_slots(self) -> dict[str, str]:
        """Get list of all managed replication slots.

        Returns: dictionary in <slot>: <database> format.
        """
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
                    and schematable in subscription_request_relation[database]
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

    def _validate_new_publication(
        self,
        database: str,
        schematables: list[str],
        publication_schematables: list[str] | None = None,
    ) -> str | None:
        if not self.charm.postgresql.database_exists(database):
            return f"database {database} doesn't exist"
        for schematable in schematables:
            if publication_schematables is not None and schematable in publication_schematables:
                continue
            schema, table = schematable.split(".")
            if not self.charm.postgresql.table_exists(database, schema, table):
                return f"table {schematable} in database {database} doesn't exist"
        return None

    def _relation_changed_checks(self, event: RelationChangedEvent) -> bool:
        if not self.charm.unit.is_leader():
            return False
        if not event.relation.data[event.app].get("secret-id"):
            logger.warning(
                f"{LOGICAL_REPLICATION_RELATION} change skipped due to secret absence in remote application data"
            )
            return False
        if not self.charm.primary_endpoint:
            event.defer()
            logger.debug(
                f"{LOGICAL_REPLICATION_RELATION} change deferred until primary is available"
            )
            return False
        return True

    def _process_offer(self, relation: Relation):
        subscriptions_request = json.loads(
            relation.data[relation.app].get("subscription-request", "{}")
        )
        publications = json.loads(relation.data[self.model.app].get("publications", "{}"))
        errors = []

        for database, publication in publications.copy().items():
            if database in subscriptions_request:
                continue
            self.charm.postgresql.drop_publication(database, publication["publication-name"])
            logger.debug(
                f"Dropped redundant publication {publication['publication-name']} from database {database}"
            )
            del publications[database]

        for database, tables in subscriptions_request.items():
            # TODO: validation
            if database not in publications:
                if validation_error := self._validate_new_publication(database, tables):
                    errors.append(validation_error)
                    logger.debug(f"Cannot create new publication: {validation_error}")
                    continue
                publication_name = self._publication_name(relation.id, database)
                self.charm.postgresql.create_publication(database, publication_name, tables)
                logger.debug(
                    f"Created new publication {publication_name} for tables {', '.join(tables)} in database {database}"
                )
                publications[database] = {
                    "publication-name": publication_name,
                    "replication-slot-name": self._replication_slot_name(relation.id, database),
                    "tables": tables,
                }
            elif sorted(publication_tables := publications[database]["tables"]) != sorted(tables):
                publication_name = publications[database]["publication-name"]
                if validation_error := self._validate_new_publication(
                    database, tables, publication_tables
                ):
                    errors.append(validation_error)
                    logger.debug(
                        f"Cannot alter publication {publication_name}: {validation_error}"
                    )
                    continue
                self.charm.postgresql.alter_publication(database, publication_name, tables)
                logger.debug(
                    f"Altered publication {publication_name} tables from {','.join(publication_tables)} to {','.join(tables)} in database {database}"
                )
                publications[database]["tables"] = tables

        relation.data[self.model.app].update({
            "errors": json.dumps(errors),
            "publications": json.dumps(publications),
        })
        self.charm.update_config()

    def _publication_name(self, relation_id: int, database: str):
        return f"relation_{relation_id}_{database}"

    def _replication_slot_name(self, relation_id: int, database: str):
        return f"relation_{relation_id}_{database}"

    def _subscription_name(self, relation_id: int, database: str):
        return f"relation_{relation_id}_{database}"

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

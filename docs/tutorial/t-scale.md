> [Charmed PostgreSQL VM Tutorial](/t/9707) > 3. Scale replicas

# Scale your replicas
In this section, you will learn to scale your Charmed PostgreSQL by adding or removing juju units. 

The Charmed PostgreSQL VM operator uses a [PostgreSQL Patroni-based cluster](https://patroni.readthedocs.io/en/latest/) for scaling. It provides features such as automatic membership management, fault tolerance, and automatic failover. The charm uses PostgreSQL’s [synchronous replication](https://patroni.readthedocs.io/en/latest/replication_modes.html#postgresql-k8s-synchronous-replication) with Patroni.

[note type="caution"]
This tutorial hosts all replicas on the same machine. 

**This should not be done in a production environment.** 

To enable high availability in a production environment, replicas should be hosted on different servers to [maintain isolation](https://canonical.com/blog/database-high-availability).
[/note]

## Summary

- [Add units](#heading--add-units)
- [Remove units](#heading--remove-units)

---

<a href="#heading--add-units"><h2 id="heading--add-units"> Add units </h2></a>

Currently, your deployment has only one juju **unit**, known in juju as the **leader unit**. You can think of this as the database **primary instance**. For each **replica**, a new unit is created. All units are members of the same database cluster.

To add two replicas to your deployed PostgreSQL application, run
```shell
juju add-unit postgresql -n 2
```

You can now watch the scaling process in live using: `juju status --watch 1s`. It usually takes several minutes for new cluster members to be added. 

You’ll know that all three nodes are in sync when `juju status` reports `Workload=active` and `Agent=idle`:
```
Model     Controller  Cloud/Region         Version  SLA          Timestamp
tutorial  overlord    localhost/localhost  3.1.7    unsupported  10:16:44+01:00

App         Version  Status  Scale  Charm       Channel    Rev  Exposed  Message
postgresql           active      3  postgresql  14/stable  281  no       

Unit           Workload  Agent  Machine  Public address  Ports  Message
postgresql/0*  active    idle   0        10.89.49.129           Primary
postgresql/1   active    idle   1        10.89.49.197           
postgresql/2   active    idle   2        10.89.49.175           

Machine  State    Address       Inst id        Series  AZ  Message
0        started  10.89.49.129  juju-a8a31d-0  jammy       Running
1        started  10.89.49.197  juju-a8a31d-1  jammy       Running
2        started  10.89.49.175  juju-a8a31d-2  jammy       Running
```

<a href="#heading--remove-units"><h2 id="heading--remove-units"> Remove units </h2></a>

Removing a unit from the application scales down the replicas.

Before we scale them down, list all the units with `juju status`. You will see three units: `postgresql/0`, `postgresql/1`, and `postgresql/2`. Each of these units hosts a PostgreSQL replica. 

To remove the replica hosted on the unit `postgresql/2` enter:
```shell
juju remove-unit postgresql/2
```

You’ll know that the replica was successfully removed when `juju status --watch 1s` reports:
```
Model     Controller  Cloud/Region         Version  SLA          Timestamp
tutorial  overlord    localhost/localhost  3.1.7    unsupported  10:17:14+01:00

App         Version  Status  Scale  Charm       Channel    Rev  Exposed  Message
postgresql           active      2  postgresql  14/stable  281  no       

Unit           Workload  Agent  Machine  Public address  Ports  Message
postgresql/0*  active    idle   0        10.89.49.129           
postgresql/1   active    idle   1        10.89.49.197           

Machine  State    Address       Inst id        Series  AZ  Message
0        started  10.89.49.129  juju-a8a31d-0  jammy       Running
1        started  10.89.49.197  juju-a8a31d-1  jammy       Running
```

**Next step:** [4. Manage passwords](/t/9703)
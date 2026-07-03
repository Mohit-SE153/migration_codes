CREATE TABLE [Person].[BusinessEntityContact] (
    [BusinessEntityID] int NOT NULL,
    [PersonID] int NOT NULL,
    [ContactTypeID] int NOT NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
